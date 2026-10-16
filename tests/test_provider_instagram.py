"""Instagram provider tests (providers/instagram.py) with respx: IMAGE / CAROUSEL / REELS / STORIES container
flows, status polling, quota, ambiguous media_publish → reconcile, idempotent first comment, analytics and
comments."""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from planificador.providers import meta_common as mc
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
from planificador.providers.instagram import STORY_METRICS, InstagramProvider
from planificador.timeutil import ManualClock

IG = "17841400000000001"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
SETTINGS = ProviderSettings(
    meta_app_id="123456",
    meta_app_secret="app-secret",
    meta_api_version="v25.0",
    tiktok_client_key=None,
    tiktok_client_secret=None,
    tiktok_pkce_encoding="hex",
    base_url="http://localhost:8765",
    demo_mode=False,
)
QUOTA_OK = {"data": [{"quota_usage": 3, "config": {"quota_total": 100, "quota_duration": 86400}}]}


# ------------------------------------------------------------------------------------------- harness


def parse_form(request: httpx.Request) -> dict[str, Any]:
    body = request.read()
    ctype = request.headers.get("content-type", "")
    if not body or not ctype.startswith("application/x-www-form-urlencoded"):
        return {}
    return {k: v[0] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()}


class FakeMeta:
    """Dispatch by (METHOD, path without version); handlers: dict | Response | exception | callable |
    several (consumed in order, the last one repeats)."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], list[Any]] = {}
        self.calls: list[tuple[str, str, dict, httpx.Request]] = []

    def on(self, method: str, path: str, *responses: Any) -> FakeMeta:
        self.handlers[(method, path)] = list(responses)
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "graph.facebook.com":
            path = re.sub(r"^/v\d+\.\d+/", "", path)
        form = parse_form(request) if request.method == "POST" else dict(request.url.params)
        self.calls.append((request.method, path, form, request))
        queue = self.handlers.get((request.method, path))
        if queue is None:
            raise AssertionError(f"Unexpected request {request.method} {path}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(response) and not isinstance(response, httpx.Response):
            response = response(request, form)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def find(self, method: str, path: str) -> list[dict]:
        return [form for m, p, form, _ in self.calls if m == method and p == path]


def error(code: int, sub: int | None = None, message: str = "error", status: int = 400) -> httpx.Response:
    body: dict[str, Any] = {"message": message, "type": "OAuthException", "code": code}
    if sub is not None:
        body["error_subcode"] = sub
    return httpx.Response(status, json={"error": body})


def timeout(request: httpx.Request, form: dict) -> Exception:
    return httpx.ReadTimeout("timed out", request=request)


class FakePublicMedia:
    def __init__(self) -> None:
        self.issued: list[tuple[Path, str, int]] = []
        self.released: list[str] = []

    def url_for(self, path: Path, mime: str, ttl_seconds: int = 3600) -> str:
        self.issued.append((Path(path), mime, ttl_seconds))
        return f"https://pub.example.com/m/tok{len(self.issued)}"

    def release(self, url: str) -> None:
        self.released.append(url)


@pytest.fixture
def fake():
    mc.reset_usage()
    meta = FakeMeta()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="graph.facebook.com").mock(side_effect=meta)
        router.route(host="rupload.facebook.com").mock(side_effect=meta)
        yield meta
    mc.reset_usage()


@pytest.fixture
def http(fake):
    with httpx.Client() as client:
        yield client


def media(tmp_path: Path, name: str, kind: str = "image", *, index: int = 1, fmt: str = "JPEG") -> MediaInfo:
    path = tmp_path / name
    if kind == "image":
        from PIL import Image

        Image.new("RGB", (1080, 1080), (20 * index, 90, 160)).save(path, fmt)
        mime = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    else:
        path.write_bytes(bytes(range(256)) * 8)
        mime = "video/mp4"
    return MediaInfo(
        id=index,
        kind=kind,
        path=path,
        mime=mime,
        size_bytes=path.stat().st_size,
        width=1080,
        height=1920 if kind == "video" else 1080,
        duration_s=12.0 if kind == "video" else None,
        fps=30.0 if kind == "video" else None,
        video_codec="h264" if kind == "video" else None,
        audio_codec="aac" if kind == "video" else None,
        bitrate=None,
        is_hdr=False,
        original_name=name,
    )


class Harness:
    def __init__(
        self,
        http: httpx.Client,
        *,
        format: str = "post",
        text: str = "Hola Instagram #prueba",
        items: list[MediaInfo] | None = None,
        options: dict | None = None,
        state: dict | None = None,
        clock: ManualClock | None = None,
        public_media: FakePublicMedia | None = None,
        no_public_media: bool = False,
        account_type: str | None = "business",
    ) -> None:
        self.saved: list[dict] = []
        self.commits: list[dict] = []
        self.events: list[tuple[str, str, Any]] = []
        self.clock = clock or ManualClock(NOW)
        self.public = None if no_public_media else (public_media or FakePublicMedia())
        self.provider = InstagramProvider()
        account = AccountCreds(
            9,
            "instagram",
            IG,
            "mimarca",
            account_type,
            "PAGETOKEN",
            None,
            None,
            None,
            {"username": "mimarca"},
        )
        target = TargetSpec("instagram", format, text, items or [], options or {}, None, True)
        self.ctx = PublishContext(
            account=account,
            target=target,
            target_id=77,
            state=state if state is not None else {},
            settings=SETTINGS,
            http=http,
            clock=self.clock,
            public_media=self.public,
            save_state=lambda s: self.saved.append(copy.deepcopy(s)),
            before_commit=lambda s: self.commits.append(copy.deepcopy(s)),
            log=lambda event, message, data: self.events.append((event, message, data)),
        )

    @property
    def state(self) -> dict:
        return self.ctx.state

    def step(self) -> StepResult:
        return self.provider.publish_step(self.ctx)

    def run(self, max_steps: int = 80) -> tuple[StepResult, list[StepResult]]:
        results: list[StepResult] = []
        for _ in range(max_steps):
            result = self.step()
            results.append(result)
            if result.status in ("published", "sent_to_app"):
                return result, results
            if result.status == "wait":
                self.clock.advance(seconds=result.wait_seconds)
        raise AssertionError("publish did not finish")

    def resumed(self, http: httpx.Client, state: dict | None = None) -> Harness:
        target = self.ctx.target
        return Harness(
            http,
            format=target.format,
            text=target.text,
            items=target.media,
            options=target.options,
            state=copy.deepcopy(state if state is not None else self.saved[-1]),
            clock=self.clock,
            public_media=self.public,
        )


def _image_routes(fake: FakeMeta, *, container: str = "C1", media_id: str = "M1") -> None:
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": container})
    fake.on("GET", container, {"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", {"id": media_id})
    fake.on("GET", media_id, {"permalink": f"https://www.instagram.com/p/{media_id}/"})


# --------------------------------------------------------------------------------------------- IMAGE


def test_image_post_happy_path_with_first_comment(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", "M1/comments", {"id": "CM1"})
    photo = media(tmp_path, "foto.jpg")
    h = Harness(
        http,
        items=[photo],
        options={
            "first_comment": "#hashtag #primer",
            "alt_text": "Una foto azul",
            "collaborators": ["@amiga", "otra", "@amiga"],
            "location_id": "12345",
        },
    )
    result, steps = h.run()
    assert [s.status for s in steps] == ["continue", "wait", "published"]
    assert steps[1].wait_seconds == 5
    assert result.external_id == "M1"
    assert result.external_url == "https://www.instagram.com/p/M1/"
    container = fake.find("POST", f"{IG}/media")[0]
    assert container["image_url"] == "https://pub.example.com/m/tok1"
    assert container["caption"] == "Hola Instagram #prueba"
    assert container["alt_text"] == "Una foto azul"
    assert json.loads(container["collaborators"]) == ["amiga", "otra"]
    assert container["location_id"] == "12345"
    assert "media_type" not in container and "upload_type" not in container
    assert container["access_token"] == "PAGETOKEN"
    assert fake.find("POST", f"{IG}/media_publish")[0]["creation_id"] == "C1"
    assert fake.find("POST", "M1/comments")[0] == {
        "message": "#hashtag #primer",
        "access_token": "PAGETOKEN",
        "appsecret_proof": mc.appsecret_proof("PAGETOKEN", "app-secret"),
    }
    issued_path, mime, ttl = h.public.issued[0]
    assert issued_path == photo.path and mime == "image/jpeg" and ttl == 3600
    assert h.public.released == ["https://pub.example.com/m/tok1"]
    assert len(h.commits) == 1 and h.commits[0]["commit_cid"] == "C1"
    assert h.state["first_comment"]["status"] == "done"
    # quota checked once; result is stable on re-entry
    assert len(fake.find("GET", f"{IG}/content_publishing_limit")) == 1
    assert h.step().external_id == "M1" and len(fake.find("POST", f"{IG}/media_publish")) == 1


def test_image_url_released_before_publish_and_png_converted(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    png = media(tmp_path, "foto.png", fmt="PNG")
    h = Harness(http, items=[png])
    h.run()
    path, mime, _ = h.public.issued[0]
    assert path.suffix == ".jpg" and path != png.path and mime == "image/jpeg"
    from PIL import Image

    with Image.open(path) as im:
        assert im.format == "JPEG"
    # released before media_publish was called
    publish_index = next(i for i, c in enumerate(fake.calls) if c[1] == f"{IG}/media_publish")
    assert h.public.released and publish_index > 0


def test_public_media_off_is_permanent(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    h = Harness(http, items=[media(tmp_path, "a.jpg")], no_public_media=True)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "public_media_off"
    assert "URL pública" in exc.value.message


def test_post_without_media_is_rejected(http):
    with pytest.raises(ProviderError) as exc:
        Harness(http).step()
    assert exc.value.code == "ig_no_media"


# ------------------------------------------------------------------------------------------ CAROUSEL


def test_carousel_children_then_parent(fake: FakeMeta, http, tmp_path):
    items = [
        media(tmp_path, "a.jpg"),
        media(tmp_path, "b.mp4", "video", index=2),
        media(tmp_path, "c.jpg", index=3),
    ]
    created = iter(
        [
            {"id": "K1"},
            {"id": "K2", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/K2"},
            {"id": "K3"},
            {"id": "P1"},
        ]
    )
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", lambda request, form: next(created))
    fake.on("POST", "/ig-api-upload/v25.0/K2", {"success": True})
    fake.on("GET", "K1", {"status_code": "FINISHED"})
    fake.on("GET", "K2", {"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"})
    fake.on("GET", "K3", {"status_code": "FINISHED"})
    fake.on("GET", "P1", {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", {"id": "M9"})
    fake.on("GET", "M9", {"permalink": "https://www.instagram.com/p/M9/"})
    h = Harness(http, items=items, options={"collaborators": "uno, @dos", "alt_text": "ignored"})
    result, steps = h.run()
    assert result.external_id == "M9"
    forms = fake.find("POST", f"{IG}/media")
    child_a, child_b, child_c, parent = forms
    assert child_a["is_carousel_item"] == "true" and child_a["image_url"].startswith(
        "https://pub.example.com"
    )
    assert "caption" not in child_a and "collaborators" not in child_a and "alt_text" not in child_a
    assert child_b["media_type"] == "VIDEO" and child_b["upload_type"] == "resumable"
    assert child_b["is_carousel_item"] == "true"
    assert parent["media_type"] == "CAROUSEL" and parent["children"] == "K1,K2,K3"
    assert parent["caption"] == "Hola Instagram #prueba"
    assert json.loads(parent["collaborators"]) == ["uno", "dos"]
    upload = [c for c in fake.calls if c[1] == "/ig-api-upload/v25.0/K2"][0][3]
    assert upload.headers["authorization"] == "OAuth PAGETOKEN"
    assert upload.headers["offset"] == "0" and upload.headers["file_size"] == str(items[1].size_bytes)
    assert upload.content == items[1].path.read_bytes()
    assert fake.find("POST", f"{IG}/media_publish")[0]["creation_id"] == "P1"
    assert len(h.public.released) == 2


def test_carousel_limits(http, tmp_path):
    many = [media(tmp_path, f"f{i}.jpg", index=i) for i in range(11)]
    with pytest.raises(ProviderError) as exc:
        Harness(http, items=many).step()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "ig_carousel_too_many"
    assert "10" in exc.value.message


def test_carousel_child_reset_also_drops_parent(fake: FakeMeta, http, tmp_path):
    items = [media(tmp_path, "a.jpg"), media(tmp_path, "b.jpg", index=2)]
    state = {
        "v": 1,
        "kind": "CAROUSEL",
        "stage": "containers",
        "quota_checked": True,
        "items": [
            {"i": 0, "media": "image", "container_id": "K1", "created_at": NOW.isoformat(), "uploaded": True},
            {"i": 1, "media": "image", "container_id": "K2", "created_at": NOW.isoformat(), "uploaded": True},
        ],
        "parent": {"container_id": "P1", "created_at": NOW.isoformat(), "media": "parent"},
        "public_urls": [],
    }
    fake.on("GET", "K1", {"status_code": "FINISHED"})
    fake.on("GET", "K2", {"status_code": "EXPIRED"})
    h = Harness(http, items=items, state=state)
    assert h.step().status == "continue"
    assert "parent" not in h.state and "container_id" not in h.state["items"][1]


# ---------------------------------------------------------------------------------------------- REELS


def _reel_routes(fake: FakeMeta, container: str = "R1") -> None:
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on(
        "POST",
        f"{IG}/media",
        {"id": container, "uri": f"https://rupload.facebook.com/ig-api-upload/v25.0/{container}"},
    )
    fake.on("POST", f"/ig-api-upload/v25.0/{container}", {"success": True})
    fake.on("POST", f"{IG}/media_publish", {"id": "MR"})
    fake.on("GET", "MR", {"permalink": "https://www.instagram.com/reel/MR/"})


def test_reel_with_all_options_and_slow_polling(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake)
    statuses = [{"status_code": "IN_PROGRESS"}] * 20 + [{"status_code": "FINISHED"}]
    fake.on("GET", "R1", *statuses)
    video = media(tmp_path, "reel.mp4", "video")
    h = Harness(
        http,
        format="reel",
        items=[video],
        options={
            "share_to_feed": False,
            "thumb_offset_ms": 1500,
            "collaborators": ["@a", "b"],
            "audio_name": "Mi audio",
            "trial_reel": "manual",
            "location_id": "777",
        },
    )
    result, steps = h.run()
    assert result.external_id == "MR" and result.external_url == "https://www.instagram.com/reel/MR/"
    form = fake.find("POST", f"{IG}/media")[0]
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable"
    assert form["share_to_feed"] == "false"
    assert form["thumb_offset"] == "1500"
    assert json.loads(form["collaborators"]) == ["a", "b"]
    assert form["audio_name"] == "Mi audio"
    assert json.loads(form["trial_params"]) == {"graduation_strategy": "MANUAL"}
    assert form["location_id"] == "777" and form["caption"] == "Hola Instagram #prueba"
    assert "image_url" not in form
    waits = [s.wait_seconds for s in steps if s.status == "wait"]
    assert waits[0] == 5 and waits[-1] == 15  # fast during the first minute, then slower
    assert h.public.issued == []  # videos never need a public URL


def test_single_video_post_is_reels_shared_to_feed(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake)
    fake.on("GET", "R1", {"status_code": "FINISHED"})
    Harness(
        http,
        items=[media(tmp_path, "v.mp4", "video")],
        options={"share_to_feed": False, "trial_reel": "MANUAL"},
    ).run()
    form = fake.find("POST", f"{IG}/media")[0]
    assert form["media_type"] == "REELS" and form["share_to_feed"] == "true"
    assert "trial_params" not in form  # trial reels only for the reel format


def test_trial_reel_unavailable_falls_back_to_normal_reel(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake)
    fake.on(
        "POST",
        f"{IG}/media",
        error(-1, 2207078, "Trial reels not available"),
        {"id": "R1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/R1"},
    )
    fake.on("GET", "R1", {"status_code": "FINISHED"})
    h = Harness(
        http,
        format="reel",
        items=[media(tmp_path, "v.mp4", "video")],
        options={"trial_reel": "SS_PERFORMANCE"},
    )
    result, _ = h.run()
    assert result.status == "published"
    first, second = fake.find("POST", f"{IG}/media")
    assert "trial_params" in first and "trial_params" not in second
    assert any(e[0] == "ig_trial_dropped" for e in h.events)


def test_rupload_failure_gets_a_new_container(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on(
        "POST",
        f"{IG}/media",
        {"id": "R1", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/R1"},
        {"id": "R2", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/R2"},
    )
    fake.on("POST", "/ig-api-upload/v25.0/R1", httpx.Response(500, json={"debug_info": {"retriable": True}}))
    fake.on("POST", "/ig-api-upload/v25.0/R2", {"success": True})
    fake.on("GET", "R2", {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", {"id": "MR"})
    fake.on("GET", "MR", {})
    h = Harness(http, format="reel", items=[media(tmp_path, "v.mp4", "video")])
    assert h.step().status == "continue"
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.TRANSIENT
    assert "container_id" not in h.state["items"][0]
    result, _ = h.run()
    assert result.external_id == "MR"
    assert fake.find("POST", f"{IG}/media_publish")[0]["creation_id"] == "R2"


def test_rupload_uri_must_be_instagram_upload_path(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": "R1", "uri": "https://rupload.facebook.com/video-upload/v25.0/R1"})
    h = Harness(http, format="reel", items=[media(tmp_path, "v.mp4", "video")])
    h.step()
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.code == "meta_bad_upload_url"


# --------------------------------------------------------------------------------------------- STORIES


def test_story_image_business_account(fake: FakeMeta, http, tmp_path):
    _image_routes(fake, container="S1", media_id="MS")
    h = Harness(http, format="story", items=[media(tmp_path, "s.jpg")], options={"first_comment": "no"})
    result, _ = h.run()
    assert result.external_id == "MS"
    form = fake.find("POST", f"{IG}/media")[0]
    assert form["media_type"] == "STORIES" and "caption" not in form and form["image_url"]
    assert not fake.find("POST", "MS/comments")  # no first comment on stories


def test_story_video_uses_resumable_upload(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake, container="S2")
    fake.on("GET", "S2", {"status_code": "FINISHED"})
    Harness(http, format="story", items=[media(tmp_path, "s.mp4", "video")]).run()
    form = fake.find("POST", f"{IG}/media")[0]
    assert form["media_type"] == "STORIES" and form["upload_type"] == "resumable"


def test_story_requires_business_account(fake: FakeMeta, http, tmp_path):
    with pytest.raises(ProviderError) as exc:
        Harness(http, format="story", items=[media(tmp_path, "s.jpg")], account_type="creator").step()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "ig_not_business"
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", error(10, None, "(#10) The user is not an Instagram Business"))
    h = Harness(http, format="story", items=[media(tmp_path, "s.jpg")], account_type=None)
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.code == "ig_not_business"
    assert h.public.released == ["https://pub.example.com/m/tok1"]


# ------------------------------------------------------------------------------------ container states


def test_container_error_permanent_releases_urls(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": "C1"})
    fake.on(
        "GET",
        "C1",
        {"status_code": "ERROR", "status": "Error: Media upload has failed with error code 2207026"},
    )
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "ig_video_format"
    assert exc.value.detail["subcode"] == 2207026
    assert h.public.released == ["https://pub.example.com/m/tok1"]
    assert "container_id" not in h.state["items"][0]
    assert not fake.find("POST", f"{IG}/media_publish")


def test_container_error_unknown_code_is_permanent(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": "C1"})
    fake.on("GET", "C1", {"status_code": "ERROR", "status": "ERROR"})
    with pytest.raises(ProviderError) as exc:
        Harness(http, items=[media(tmp_path, "a.jpg")]).run()
    assert exc.value.code == "ig_container_error"


def test_container_error_transient_recreates_container(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": "C1"}, {"id": "C2"})
    fake.on("GET", "C1", {"status_code": "ERROR", "status": "Media download timed out (2207003)"})
    fake.on("GET", "C2", {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", {"id": "M2"})
    fake.on("GET", "M2", {})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "ig_media_fetch_failed"
    assert h.public.released == []  # the URL is reused for the new container
    result, _ = h.run()
    assert result.external_id == "M2"
    assert [f["image_url"] for f in fake.find("POST", f"{IG}/media")] == [
        "https://pub.example.com/m/tok1"
    ] * 2


def test_expired_container_is_recreated(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", {"id": "C1"}, {"id": "C2"})
    fake.on("GET", "C1", {"status_code": "EXPIRED"})
    fake.on("GET", "C2", {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", {"id": "M3"})
    fake.on("GET", "M3", {})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    result, _ = h.run()
    assert result.external_id == "M3"
    assert fake.find("POST", f"{IG}/media_publish")[0]["creation_id"] == "C2"


def test_old_container_is_renewed_before_use(fake: FakeMeta, http, tmp_path):
    _image_routes(fake, container="C9")
    state = {
        "v": 1,
        "kind": "IMAGE",
        "stage": "containers",
        "quota_checked": True,
        "items": [
            {
                "i": 0,
                "media": "image",
                "container_id": "OLD",
                "uploaded": True,
                "created_at": (NOW - timedelta(hours=30)).isoformat(),
            }
        ],
        "public_urls": [],
    }
    h = Harness(http, items=[media(tmp_path, "a.jpg")], state=state)
    h.run()
    assert fake.find("POST", f"{IG}/media_publish")[0]["creation_id"] == "C9"


def test_processing_timeout(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake)
    fake.on("GET", "R1", {"status_code": "IN_PROGRESS"})
    h = Harness(http, format="reel", items=[media(tmp_path, "v.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run(max_steps=200)
    assert exc.value.code == "ig_processing_timeout" and exc.value.kind == ErrorKind.TRANSIENT
    assert "container_id" not in h.state["items"][0]


# --------------------------------------------------------------------------------------------- quota


def test_quota_reached_is_rate_limited(fake: FakeMeta, http, tmp_path):
    fake.on(
        "GET",
        f"{IG}/content_publishing_limit",
        {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]},
    )
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.RATE_LIMITED and exc.value.retry_after == 3600
    assert "100 de 100" in exc.value.message
    assert not fake.find("POST", f"{IG}/media")


def test_quota_check_failure_is_not_blocking(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("GET", f"{IG}/content_publishing_limit", error(2, status=500))
    result, _ = Harness(http, items=[media(tmp_path, "a.jpg")]).run()
    assert result.status == "published"


def test_quota_error_2207042_at_publish(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(9, 2207042, "Max posts reached"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    assert h.provider.reconcile(h.ctx).outcome == "not_published"


# ------------------------------------------------------------------------------ commit / reconcile


def test_ambiguous_publish_reconciles_published_and_posts_first_comment(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"first_comment": "¡Primero!"})
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    assert h.state["stage"] == "committing" and len(h.commits) == 1
    fake.on("GET", "C1", {"status_code": "PUBLISHED"})
    fake.on(
        "GET",
        f"{IG}/media",
        {
            "data": [
                {"id": "OTHER", "caption": "otra", "timestamp": "2026-11-03T08:59:00+0000"},
                {
                    "id": "M1",
                    "caption": "Hola  Instagram #prueba",
                    "timestamp": "2026-11-03T09:00:05+0000",
                    "permalink": "https://www.instagram.com/p/M1/",
                },
            ]
        },
    )
    fake.on("GET", "M1/comments", {"data": []})
    fake.on("POST", "M1/comments", {"id": "CM"})
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published"
    assert rec.external_id == "M1" and rec.external_url == "https://www.instagram.com/p/M1/"
    assert h.state["first_comment"]["status"] == "done"
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1  # never re-sent


def test_ambiguous_publish_reconcile_not_published_then_retry(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on(
        "POST",
        f"{IG}/media_publish",
        httpx.Response(500, json={"error": {"code": 2, "message": "x"}}),
        {"id": "M1"},
    )
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    fake.on("GET", "C1", {"status_code": "FINISHED"})
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "not_published" and h.state["stage"] == "commit"
    result, _ = h.run()
    assert result.external_id == "M1" and len(h.commits) == 2


def test_reconcile_unknown_when_container_lookup_fails(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "C1", error(2, status=500))
    assert h.provider.reconcile(h.ctx).outcome == "unknown"
    assert Harness(http).provider.reconcile(Harness(http).ctx).outcome == "unknown"


def test_crash_after_before_commit_does_not_publish_twice(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "C1", {"status_code": "PUBLISHED"})
    fake.on("GET", f"{IG}/media", {"data": []})
    resumed = h.resumed(http, state=h.commits[-1])
    result = resumed.step()
    assert result.status == "published" and result.external_id is None
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1


def test_media_not_ready_at_publish_waits_and_retries(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(9007, 2207027, "Media ID is not available"), {"id": "M1"})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    result, steps = h.run()
    assert result.external_id == "M1"
    assert any(s.status == "wait" and s.wait_seconds == 30 for s in steps)
    assert len(h.commits) == 2


def test_invalid_container_at_publish_recreates(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media", {"id": "C1"}, {"id": "C5"})
    fake.on("GET", "C5", {"status_code": "FINISHED"})
    fake.on("POST", f"{IG}/media_publish", error(-1, 2207006, "media not found"), {"id": "M5"})
    fake.on("GET", "M5", {})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.TRANSIENT
    assert h.provider.reconcile(h.ctx).outcome == "not_published"
    result, _ = h.run()
    assert result.external_id == "M5"
    assert [f["creation_id"] for f in fake.find("POST", f"{IG}/media_publish")] == ["C1", "C5"]


def test_auth_error_on_publish_releases_urls(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(190, 460))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AUTH
    assert h.public.released


def test_throttled_container_creation(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", error(4, None, "Application request limit reached"))
    with pytest.raises(ProviderError) as exc:
        Harness(http, format="reel", items=[media(tmp_path, "v.mp4", "video")]).run()
    assert exc.value.kind == ErrorKind.RATE_LIMITED


# ------------------------------------------------------------------------------------- first comment


def test_first_comment_1772107_looks_up_then_retries_once(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", "M1/comments", error(20, 1772107, "comment error"), {"id": "CM2"})
    fake.on("GET", "M1/comments", {"data": [{"id": "x", "text": "otro", "username": "fan"}]})
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"first_comment": "Hola"})
    result, _ = h.run()
    assert result.status == "published"
    assert len(fake.find("POST", "M1/comments")) == 2
    assert "fields" not in fake.find("POST", "M1/comments")[0]
    assert h.state["first_comment"] == {
        "status": "done",
        "attempts": 2,
        "id": "CM2",
        "error": mc.SUBCODE_MAP[1772107][2],
    }


def test_first_comment_found_after_timeout_is_not_duplicated(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", "M1/comments", timeout)
    fake.on(
        "GET",
        "M1/comments",
        {"data": [{"id": "CM3", "text": "Hola", "from": {"id": IG, "username": "mimarca"}}]},
    )
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"first_comment": "Hola"})
    result, _ = h.run()
    assert result.status == "published" and h.state["first_comment"]["id"] == "CM3"
    assert len(fake.find("POST", "M1/comments")) == 1


def test_first_comment_failure_never_fails_the_publish(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", "M1/comments", error(100, None, "Invalid parameter"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"first_comment": "Hola"})
    result, _ = h.run()
    assert result.status == "published" and "primer comentario" in (result.message or "")
    assert h.state["first_comment"]["status"] == "failed"
    assert len(fake.find("POST", "M1/comments")) == 1
    assert any(e[0] == "ig_first_comment_failed" for e in h.events)


def test_first_comment_reentry_in_sending_state(fake: FakeMeta, http, tmp_path):
    fake.on("GET", "M1/comments", {"data": [{"id": "CM4", "text": "Hola", "username": "mimarca"}]})
    state = {
        "v": 1,
        "kind": "IMAGE",
        "items": [],
        "media_id": "M1",
        "permalink_checked": True,
        "permalink": "https://www.instagram.com/p/M1/",
        "first_comment": {"status": "sending", "attempts": 1},
    }
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"first_comment": "Hola"}, state=state)
    result = h.step()
    assert result.external_id == "M1" and h.state["first_comment"]["id"] == "CM4"
    assert not fake.find("POST", "M1/comments")


# ----------------------------------------------------------------------------------------- analytics


def _account() -> AccountCreds:
    return AccountCreds(
        9, "instagram", IG, "mimarca", "business", "PT", None, None, None, {"username": "mimarca"}
    )


def test_account_snapshot_maps_2026_metrics(fake: FakeMeta, http):
    day = date(2026, 11, 2)

    def insights(request, form):
        metric = form["metric"]
        if metric == "reach":
            assert "metric_type" not in form
            return {
                "data": [
                    {
                        "name": "reach",
                        "period": "day",
                        "values": [{"value": 800, "end_time": "2026-11-03T08:00:00+0000"}],
                    }
                ]
            }
        assert form["metric_type"] == "total_value"
        if "," in metric:
            return error(
                100, None, "(#100) metric[3] must be one of the following values... valid insights metric"
            )
        values = {"views": 5000, "total_interactions": 240}
        return {"data": [{"name": metric, "period": "day", "total_value": {"value": values[metric]}}]}

    fake.on("GET", IG, {"followers_count": 3100, "follows_count": 210, "media_count": 87})
    fake.on("GET", f"{IG}/insights", insights)
    snap = InstagramProvider().fetch_account_snapshot(_account(), SETTINGS, http, day)
    assert snap.metrics == {
        "followers": 3100,
        "following": 210,
        "posts_count": 87,
        "reach": 800,
        "views": 5000,
        "interactions": 240,
    }
    assert "impressions" not in json.dumps([f.get("metric") for _, _, f, _ in fake.calls])


def test_recent_posts_with_insights(fake: FakeMeta, http):
    since = NOW - timedelta(days=30)
    fake.on(
        "GET",
        f"{IG}/media",
        {
            "data": [
                {
                    "id": "A",
                    "caption": "reel",
                    "media_type": "VIDEO",
                    "media_product_type": "REELS",
                    "thumbnail_url": "https://cdn/a.jpg",
                    "permalink": "https://www.instagram.com/reel/A/",
                    "timestamp": "2026-11-01T10:00:00+0000",
                    "like_count": 50,
                    "comments_count": 4,
                },
                {
                    "id": "B",
                    "caption": "foto",
                    "media_type": "IMAGE",
                    "media_product_type": "FEED",
                    "media_url": "https://cdn/b.jpg",
                    "timestamp": "2026-10-20T10:00:00+0000",
                    "like_count": 10,
                    "comments_count": 0,
                },
                {"id": "OLD", "timestamp": "2026-09-01T10:00:00+0000"},
            ]
        },
    )
    fake.on(
        "GET",
        "A/insights",
        {
            "data": [
                {"name": "views", "values": [{"value": 4000}]},
                {"name": "reach", "values": [{"value": 2500}]},
                {"name": "likes", "values": [{"value": 50}]},
                {"name": "comments", "values": [{"value": 4}]},
                {"name": "shares", "values": [{"value": 6}]},
                {"name": "saved", "values": [{"value": 15}]},
                {"name": "total_interactions", "values": [{"value": 75}]},
            ]
        },
    )
    fake.on("GET", "B/insights", error(10, None, "(#10) Not enough viewers for the media to show insights"))
    fake.on("GET", f"{IG}/stories", {"data": []})
    posts = InstagramProvider().fetch_recent_posts(_account(), SETTINGS, http, since)
    assert [p.external_id for p in posts] == ["A", "B"]
    reel, photo = posts
    assert reel.media_type == "REEL" and reel.thumbnail_url == "https://cdn/a.jpg"
    assert reel.metrics == {
        "likes": 50,
        "comments": 4,
        "views": 4000,
        "reach": 2500,
        "shares": 6,
        "saves": 15,
        "interactions": 75,
        "engagement": 3.0,
    }
    assert photo.media_type == "IMAGE" and photo.thumbnail_url == "https://cdn/b.jpg"
    assert photo.metrics == {"likes": 10, "comments": 0, "interactions": 10}


# ------------------------------------------------------------------------------------------ comments


def test_fetch_comments_with_replies_and_degraded_fields(fake: FakeMeta, http):
    since = NOW - timedelta(days=3)
    fake.on(
        "GET",
        f"{IG}/media",
        {
            "data": [
                {
                    "id": "A",
                    "timestamp": "2026-11-02T10:00:00+0000",
                    "permalink": "https://ig/A",
                    "comments_count": 2,
                },
                {
                    "id": "Z",
                    "timestamp": "2026-11-01T10:00:00+0000",
                    "permalink": "https://ig/Z",
                    "comments_count": 0,
                },
            ]
        },
    )

    def comments(request, form):
        if "from{" in form["fields"]:
            return error(10, None, "(#10) Application does not have permission for this action")
        return {
            "data": [
                {
                    "id": "c1",
                    "text": "¡Me encanta!",
                    "timestamp": "2026-11-02T11:00:00+0000",
                    "username": "fan1",
                    "replies": {
                        "data": [
                            {
                                "id": "c2",
                                "text": "¡Gracias!",
                                "timestamp": "2026-11-02T12:00:00+0000",
                                "username": "mimarca",
                            }
                        ]
                    },
                },
                {"id": "c0", "text": "viejo", "timestamp": "2026-10-01T11:00:00+0000", "username": "x"},
            ]
        }

    fake.on("GET", "A/comments", comments)
    result = InstagramProvider().fetch_comments(_account(), SETTINGS, http, since)
    assert [c.external_id for c in result] == ["c1", "c2"]
    top, reply = result
    assert top.author_name == "fan1" and not top.from_us and top.parent_external_id is None
    assert top.object_external_id == "A" and top.object_permalink == "https://ig/A"
    assert reply.parent_external_id == "c1" and reply.from_us
    assert "replies" not in top.raw
    assert len(fake.find("GET", "A/comments")) == 2  # degraded once


def test_reply_comment_uses_replies_edge_and_parent(fake: FakeMeta, http):
    fake.on("GET", "c2", {"id": "c2", "parent_id": "c1"})
    fake.on("GET", "c1", {"id": "c1"})
    fake.on("POST", "c1/replies", {"id": "r1"})
    provider = InstagramProvider()
    assert provider.reply_comment(_account(), SETTINGS, http, "c2", "¡Gracias!") == "r1"
    assert provider.reply_comment(_account(), SETTINGS, http, "c1", "Hola") == "r1"
    assert [f["message"] for f in fake.find("POST", "c1/replies")] == ["¡Gracias!", "Hola"]
    with pytest.raises(ProviderError):
        provider.reply_comment(_account(), SETTINGS, http, "c1", "")


def test_check_account_and_refresh(fake: FakeMeta, http):
    fake.on("GET", IG, {"id": IG, "username": "mimarca"}, error(190, 463))
    provider = InstagramProvider()
    provider.check_account(_account(), SETTINGS, http)
    with pytest.raises(ProviderError) as exc:
        provider.check_account(_account(), SETTINGS, http)
    assert exc.value.kind == ErrorKind.AUTH
    assert provider.refresh_credentials(_account(), SETTINGS, http, ManualClock(NOW)) is None
    assert [f.key for f in provider.formats] == ["post", "reel", "story"] and provider.supports_comments


def test_story_reconcile_uses_stories_edge(fake: FakeMeta, http, tmp_path):
    _image_routes(fake, container="S1", media_id="MS")
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, format="story", items=[media(tmp_path, "s.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "S1", {"status_code": "PUBLISHED"})
    fake.on("GET", f"{IG}/stories", {"data": [{"id": "ST1", "timestamp": "2026-11-03T09:00:02+0000"}]})
    fake.on("GET", "ST1", {"permalink": "https://www.instagram.com/stories/mimarca/ST1/"})
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == "ST1"
    assert rec.external_url == "https://www.instagram.com/stories/mimarca/ST1/"


def test_reconcile_error_container_resets_for_a_clean_retry(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "C1", {"status_code": "EXPIRED"})
    assert h.provider.reconcile(h.ctx).outcome == "not_published"
    assert "container_id" not in h.state["items"][0] and "commit_cid" not in h.state


def test_guard_rechecks_container_then_recommits(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout, {"id": "M1"})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "C1", {"status_code": "FINISHED"})
    resumed = h.resumed(http, state=h.commits[-1])
    result, _ = resumed.run()
    assert result.external_id == "M1" and len(fake.find("POST", f"{IG}/media_publish")) == 2


def test_guard_unknown_container_state_raises_ambiguous(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", timeout)
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    fake.on("GET", "C1", {"status_code": "SOMETHING_NEW"})
    with pytest.raises(ProviderError) as exc:
        h.resumed(http, state=h.commits[-1]).step()
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "ig_commit_unknown"
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1


# ------------------------------------------------------------------- review regressions (meta-conformance)


def test_action_block_2207051_on_publish_reads_container_and_stops(fake: FakeMeta, http, tmp_path):
    """Meta sends the action block as code 4 / 2207051: it is not a plain rate limit, the container is read
    before deciding and the run stops (24 h hold) instead of re-sending media_publish."""
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(4, 2207051, "We restrict certain activity"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "ig_activity_restricted"
    assert "24 horas" in exc.value.message
    assert len(fake.find("GET", "C1")) == 3  # 2 polls + the check right after the refusal
    assert h.state.get("commit_rejected") is True and h.public.released
    assert h.provider.reconcile(h.ctx).outcome == "not_published"
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1


def test_action_block_2207051_when_the_post_went_live_is_published(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("GET", "C1", {"status_code": "FINISHED"}, {"status_code": "PUBLISHED"})
    fake.on("POST", f"{IG}/media_publish", error(4, 2207051, "We restrict certain activity"))
    fake.on(
        "GET",
        f"{IG}/media",
        {
            "data": [
                {
                    "id": "M9",
                    "caption": "Hola Instagram #prueba",
                    "timestamp": "2026-11-03T09:00:03+0000",
                    "permalink": "https://www.instagram.com/p/M9/",
                }
            ]
        },
    )
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    result, _ = h.run()
    assert result.status == "published" and result.external_id == "M9"
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1
    assert any(event == "ig_publish_recovered" for event, _, _ in h.events)


def test_action_block_with_unreadable_container_is_ambiguous(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("GET", "C1", {"status_code": "IN_PROGRESS"}, {"status_code": "FINISHED"}, error(2, status=500))
    fake.on("POST", f"{IG}/media_publish", error(4, 2207051, "We restrict certain activity"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "ig_activity_restricted"
    assert exc.value.retry_after == mc.ACTION_BLOCK_WAIT
    assert not h.state.get("commit_rejected") and h.state["stage"] == "committing"
    fake.on("GET", "C1", {"status_code": "FINISHED"})
    assert h.provider.reconcile(h.ctx).outcome == "not_published"


def test_retry_after_refused_publish_rereads_container_before_resending(fake: FakeMeta, http, tmp_path):
    """A refusal marked commit_rejected must not make the next attempt trust the stale FINISHED status: the
    container is read again and a post that went live is never published twice."""
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(4, None, "Application request limit reached"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    assert h.provider.reconcile(h.ctx).outcome == "not_published"
    fake.on("GET", "C1", {"status_code": "PUBLISHED"})
    fake.on("GET", f"{IG}/media", {"data": []})
    gets = len(fake.find("GET", "C1"))
    result = h.step()
    assert result.status == "published"
    assert len(fake.find("GET", "C1")) == gets + 1
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1


def test_retry_after_refused_publish_resends_when_container_still_finished(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(9, 2207042, "Max posts reached"), {"id": "M1"})
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h.run()
    assert h.provider.reconcile(h.ctx).outcome == "not_published"
    gets = len(fake.find("GET", "C1"))
    result, _ = h.run()
    assert result.external_id == "M1" and len(fake.find("GET", "C1")) == gets + 1
    assert len(fake.find("POST", f"{IG}/media_publish")) == 2


def test_server_error_2207001_on_publish_is_ambiguous_not_spam(fake: FakeMeta, http, tmp_path):
    _image_routes(fake)
    fake.on("POST", f"{IG}/media_publish", error(-1, 2207001, "Instagram server error"))
    h = Harness(http, items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "ig_server_error"
    assert "spam" not in exc.value.message
    fake.on("GET", "C1", {"status_code": "PUBLISHED"})
    fake.on("GET", f"{IG}/media", {"data": []})
    assert h.provider.reconcile(h.ctx).outcome == "published"
    assert len(fake.find("POST", f"{IG}/media_publish")) == 1


def test_server_error_2207001_on_container_is_retried(fake: FakeMeta, http, tmp_path):
    fake.on("GET", f"{IG}/content_publishing_limit", QUOTA_OK)
    fake.on("POST", f"{IG}/media", error(-1, 2207001, "Instagram server error"))
    with pytest.raises(ProviderError) as exc:
        Harness(http, items=[media(tmp_path, "a.jpg")]).run()
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.retry_after == mc.IG_SERVER_ERROR_WAIT


def test_recent_posts_include_live_stories_with_story_metrics(fake: FakeMeta, http):
    """/media never lists stories: they come from /stories and need their insights before they expire."""
    fake.on(
        "GET",
        f"{IG}/media",
        {"data": [{"id": "F1", "media_type": "IMAGE", "timestamp": "2026-11-02T10:00:00+0000"}]},
    )
    fake.on("GET", "F1/insights", {"data": [{"name": "views", "values": [{"value": 10}]}]})
    fake.on(
        "GET",
        f"{IG}/stories",
        {
            "data": [
                {
                    "id": "ST1",
                    "media_type": "IMAGE",
                    "media_product_type": "STORY",
                    "media_url": "https://cdn/st1.jpg",
                    "permalink": "https://www.instagram.com/stories/mimarca/ST1/",
                    "timestamp": "2026-11-03T07:00:00+0000",
                }
            ]
        },
    )
    fake.on(
        "GET",
        "ST1/insights",
        {
            "data": [
                {"name": "views", "values": [{"value": 300}]},
                {"name": "reach", "values": [{"value": 250}]},
                {"name": "shares", "values": [{"value": 2}]},
                {"name": "total_interactions", "values": [{"value": 9}]},
            ]
        },
    )
    posts = InstagramProvider().fetch_recent_posts(_account(), SETTINGS, http, NOW - timedelta(days=30))
    assert [p.external_id for p in posts] == ["F1", "ST1"]
    story = posts[1]
    assert story.media_type == "STORY" and story.thumbnail_url == "https://cdn/st1.jpg"
    assert story.metrics["views"] == 300 and story.metrics["reach"] == 250
    assert story.metrics["interactions"] == 9
    asked = fake.find("GET", "ST1/insights")[0]["metric"]
    assert asked == ",".join(STORY_METRICS)
    stories_call = fake.find("GET", f"{IG}/stories")[0]
    assert "timestamp" in stories_call["fields"] and "media_product_type" in stories_call["fields"]


def test_recent_posts_stories_failure_keeps_feed_posts(fake: FakeMeta, http):
    fake.on(
        "GET",
        f"{IG}/media",
        {"data": [{"id": "F1", "media_type": "IMAGE", "timestamp": "2026-11-02T10:00:00+0000"}]},
    )
    fake.on("GET", "F1/insights", {"data": []})
    fake.on("GET", f"{IG}/stories", error(10, None, "(#10) Application does not have permission"))
    posts = InstagramProvider().fetch_recent_posts(_account(), SETTINGS, http, NOW - timedelta(days=30))
    assert [p.external_id for p in posts] == ["F1"]

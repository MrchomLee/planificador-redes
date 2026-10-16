"""Facebook Page provider tests (providers/facebook.py) with respx: every publish flow and state transition,
native scheduling, throttling/auth errors, ambiguous commits → reconcile, analytics and comments."""

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
from planificador.providers.facebook import FacebookProvider
from planificador.timeutil import ManualClock

PAGE = "111"
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


# ------------------------------------------------------------------------------------------- harness


def parse_form(request: httpx.Request) -> dict[str, Any]:
    body = request.read()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        boundary = ctype.split("boundary=", 1)[1].encode()
        out: dict[str, Any] = {}
        for part in body.split(b"--" + boundary):
            head, sep, value = part.partition(b"\r\n\r\n")
            if not sep:
                continue
            match = re.search(rb'name="([^"]+)"', head)
            if not match:
                continue
            value = value[:-2] if value.endswith(b"\r\n") else value
            out[match.group(1).decode()] = value if b"filename=" in head else value.decode()
        return out
    if not body or not ctype.startswith("application/x-www-form-urlencoded"):
        return {}
    return {k: v[0] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()}


class FakeMeta:
    """Dispatches requests by (METHOD, path without version). A handler is a dict (JSON 200), an
    ``httpx.Response``, an exception, a callable(request, form) or a list of those (consumed in order; the
    last one repeats)."""

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


def media(
    tmp_path: Path, name: str, kind: str = "image", *, index: int = 1, size: int | None = None
) -> MediaInfo:
    path = tmp_path / name
    if kind == "image":
        from PIL import Image

        Image.new("RGB", (640, 640), (10 * index, 120, 200)).save(path, "JPEG")
        mime = "image/jpeg"
    else:
        path.write_bytes(bytes(range(256)) * ((size or 2560) // 256))
        mime = "video/mp4"
    return MediaInfo(
        id=index,
        kind=kind,
        path=path,
        mime=mime,
        size_bytes=path.stat().st_size,
        width=640,
        height=640,
        duration_s=10.0 if kind == "video" else None,
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
        text: str = "Hola mundo",
        items: list[MediaInfo] | None = None,
        options: dict | None = None,
        scheduled_at: datetime | None = None,
        state: dict | None = None,
        clock: ManualClock | None = None,
        token: str | None = "PAGETOKEN",
    ) -> None:
        self.saved: list[dict] = []
        self.commits: list[dict] = []
        self.events: list[tuple[str, str, Any]] = []
        self.clock = clock or ManualClock(NOW)
        self.provider = FacebookProvider()
        account = AccountCreds(7, "facebook", PAGE, "Mi Página", "page", token, None, None, None, {})
        target = TargetSpec("facebook", format, text, items or [], options or {}, scheduled_at, True)
        self.ctx = PublishContext(
            account=account,
            target=target,
            target_id=42,
            state=state if state is not None else {},
            settings=SETTINGS,
            http=http,
            clock=self.clock,
            public_media=None,
            save_state=lambda s: self.saved.append(copy.deepcopy(s)),
            before_commit=lambda s: self.commits.append(copy.deepcopy(s)),
            log=lambda event, message, data: self.events.append((event, message, data)),
        )

    @property
    def state(self) -> dict:
        return self.ctx.state

    def step(self) -> StepResult:
        return self.provider.publish_step(self.ctx)

    def run(self, max_steps: int = 60) -> tuple[StepResult, list[StepResult]]:
        results: list[StepResult] = []
        for _ in range(max_steps):
            result = self.step()
            results.append(result)
            if result.status in ("published", "sent_to_app"):
                return result, results
            if result.status == "wait":
                self.clock.advance(seconds=result.wait_seconds)
        raise AssertionError("publish did not finish")

    def resumed(self, http: httpx.Client, **kwargs: Any) -> Harness:
        """A new context from the last persisted state (simulates a crash/restart)."""
        state = copy.deepcopy(self.saved[-1] if self.saved else self.state)
        target = self.ctx.target
        return Harness(
            http,
            format=target.format,
            text=target.text,
            items=target.media,
            options=target.options,
            scheduled_at=target.scheduled_at,
            state=state,
            clock=self.clock,
            **kwargs,
        )


# --------------------------------------------------------------------------------------- text & link


def test_text_post_happy_path(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_900"})
    fake.on("GET", f"{PAGE}_900", {"permalink_url": "https://www.facebook.com/111/posts/900"})
    h = Harness(http, text="Hola mundo", options={"link": "https://example.com"})
    result, steps = h.run()
    assert result.external_id == f"{PAGE}_900"
    assert result.external_url == "https://www.facebook.com/111/posts/900"
    form = fake.find("POST", f"{PAGE}/feed")[0]
    assert form["message"] == "Hola mundo"
    assert form["link"] == "https://example.com"
    assert form["access_token"] == "PAGETOKEN"
    assert form["appsecret_proof"] == mc.appsecret_proof("PAGETOKEN", "app-secret")
    assert "published" not in form
    assert len(h.commits) == 1 and h.commits[0]["stage"] == "committing"
    assert h.state["result"]["external_id"] == f"{PAGE}_900"
    # Re-entry after success never posts again.
    assert h.step().status == "published"
    assert len(fake.find("POST", f"{PAGE}/feed")) == 1


def test_text_post_permalink_failure_is_not_fatal(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_901"})
    fake.on("GET", f"{PAGE}_901", error(2, status=500))
    result, _ = Harness(http).run()
    assert result.status == "published" and result.external_url is None


def test_link_scrape_failure_retries_with_link_in_text(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", error(100, 1609005, "Link could not be scraped"), {"id": f"{PAGE}_902"})
    fake.on("GET", f"{PAGE}_902", {})
    h = Harness(http, text="Mira esto", options={"link": "https://example.com/a"})
    result, _ = h.run()
    assert result.external_id == f"{PAGE}_902"
    first, second = fake.find("POST", f"{PAGE}/feed")
    assert first["link"] == "https://example.com/a"
    assert "link" not in second and second["message"] == "Mira esto\n\nhttps://example.com/a"
    assert any(e[0] == "fb_link_dropped" for e in h.events)


def test_empty_post_is_permanent(http):
    with pytest.raises(ProviderError) as exc:
        Harness(http, text="  ").step()
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "fb_empty"


# --------------------------------------------------------------------------------------------- photos


def test_single_photo_post_uploads_source_as_commit(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "555", "post_id": f"{PAGE}_903"})
    fake.on("GET", f"{PAGE}_903", {"permalink_url": "https://www.facebook.com/photo/903"})
    photo = media(tmp_path, "foto.jpg")
    h = Harness(http, text="Foto", items=[photo])
    result, _ = h.run()
    assert result.external_id == f"{PAGE}_903"
    form = fake.find("POST", f"{PAGE}/photos")[0]
    assert form["published"] == "true" and form["message"] == "Foto"
    assert form["source"] == photo.path.read_bytes()
    assert len(h.commits) == 1


def test_multi_photo_post_stages_then_attaches(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "p1"}, {"id": "p2"}, {"id": "p3"})
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_904"})
    fake.on("GET", f"{PAGE}_904", {"permalink_url": "https://fb/904"})
    photos = [media(tmp_path, f"f{i}.jpg", index=i) for i in range(1, 4)]
    h = Harness(http, text="Álbum", items=photos)
    result, steps = h.run()
    assert [s.status for s in steps] == ["continue", "continue", "continue", "published"]
    assert result.external_id == f"{PAGE}_904"
    uploads = fake.find("POST", f"{PAGE}/photos")
    assert [u["published"] for u in uploads] == ["false"] * 3
    assert all("temporary" not in u for u in uploads)
    feed = fake.find("POST", f"{PAGE}/feed")[0]
    assert [json.loads(feed[f"attached_media[{i}]"]) for i in range(3)] == [
        {"media_fbid": "p1"},
        {"media_fbid": "p2"},
        {"media_fbid": "p3"},
    ]
    # each staged id was persisted before the next upload (re-entrant)
    assert h.saved[1]["photo_ids"] == ["p1", None, None]
    assert len(h.commits) == 1 and h.commits[0]["photo_ids"] == ["p1", "p2", "p3"]


def test_multi_photo_resume_after_crash_does_not_reupload(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "p1"}, error(2, status=500), {"id": "p2"})
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_905"})
    fake.on("GET", f"{PAGE}_905", {})
    photos = [media(tmp_path, f"f{i}.jpg", index=i) for i in range(1, 3)]
    h = Harness(http, items=photos)
    assert h.step().status == "continue"
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.TRANSIENT
    h2 = h.resumed(http)
    result, _ = h2.run()
    assert result.external_id == f"{PAGE}_905"
    assert len(fake.find("POST", f"{PAGE}/photos")) == 3  # p1 once, failed attempt, p2


def test_multi_photo_permanent_rejection_deletes_staged_photos(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "p1"}, {"id": "p2"})
    fake.on("POST", f"{PAGE}/feed", error(368, 1346003, "Blocked"))
    fake.on("DELETE", "p1", {"success": True})
    fake.on("DELETE", "p2", {"success": True})
    h = Harness(http, items=[media(tmp_path, "a.jpg"), media(tmp_path, "b.jpg", index=2)])
    h.step()
    h.step()
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.code == "fb_content_blocked"
    assert len(fake.find("DELETE", "p1")) == 1 and len(fake.find("DELETE", "p2")) == 1
    assert h.state["photo_ids"] == [] and h.state["commit_rejected"] is True
    assert h.provider.reconcile(h.ctx).outcome == "not_published"


def test_too_many_photos_or_mixed_media(http, tmp_path):
    photos = [media(tmp_path, f"f{i}.jpg", index=i) for i in range(11)]
    with pytest.raises(ProviderError) as exc:
        Harness(http, items=photos).step()
    assert exc.value.code == "fb_too_many_photos"
    with pytest.raises(ProviderError) as exc:
        Harness(http, items=[photos[0], media(tmp_path, "v.mp4", "video")]).step()
    assert exc.value.code == "fb_mixed_media"


def test_large_png_is_converted_to_jpeg_under_4mb(fake: FakeMeta, http, tmp_path, monkeypatch):
    from planificador.providers import facebook

    monkeypatch.setattr(facebook, "PHOTO_MAX_BYTES", 20_000)
    from PIL import Image

    path = tmp_path / "big.png"
    Image.effect_noise((600, 600), 80).convert("RGB").save(path, "PNG")
    info = media(tmp_path, "x.jpg")
    info.path, info.mime, info.size_bytes = path, "image/png", path.stat().st_size
    assert info.size_bytes > 20_000
    fake.on("POST", f"{PAGE}/photos", {"id": "9", "post_id": f"{PAGE}_906"})
    fake.on("GET", f"{PAGE}_906", {})
    Harness(http, items=[info]).run()
    upload = fake.calls[0][3]
    assert b"image/jpeg" in upload.content and len(fake.find("POST", f"{PAGE}/photos")[0]["source"]) <= 20_000


# ------------------------------------------------------------------------------------ native schedule


def test_native_schedule_text_post(fake: FakeMeta, http):
    when = NOW + timedelta(days=2)
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_910"})
    fake.on("GET", f"{PAGE}_910", {"permalink_url": "https://fb/910"})
    h = Harness(http, options={"native_schedule": True}, scheduled_at=when)
    result, _ = h.run()
    assert result.status == "published" and result.external_id == f"{PAGE}_910"
    assert "Programado en Facebook" in (result.message or "")
    assert h.state["native_scheduled"] is True
    form = fake.find("POST", f"{PAGE}/feed")[0]
    assert form["published"] == "false"
    assert form["scheduled_publish_time"] == str(int(when.timestamp()))


def test_native_schedule_photos_are_temporary(fake: FakeMeta, http, tmp_path):
    when = NOW + timedelta(hours=3)
    fake.on("POST", f"{PAGE}/photos", {"id": "p1"})
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_911"})
    fake.on("GET", f"{PAGE}_911", {})
    h = Harness(http, items=[media(tmp_path, "a.jpg")], options={"native_schedule": True}, scheduled_at=when)
    result, _ = h.run()
    assert h.state["flow"] == "photos" and h.state["native_scheduled"] is True
    upload = fake.find("POST", f"{PAGE}/photos")[0]
    assert upload["temporary"] == "true" and upload["published"] == "false"
    feed = fake.find("POST", f"{PAGE}/feed")[0]
    assert feed["scheduled_publish_time"] == str(int(when.timestamp()))


def test_native_schedule_windows(fake: FakeMeta, http):
    far = Harness(http, options={"native_schedule": True}, scheduled_at=NOW + timedelta(days=40))
    result = far.step()
    assert result.status == "wait"
    assert result.wait_seconds == int(timedelta(days=11).total_seconds()) + 60
    assert "flow" not in far.state

    soon = Harness(http, options={"native_schedule": True}, scheduled_at=NOW + timedelta(minutes=4))
    result = soon.step()
    assert result.status == "wait" and result.wait_seconds == 240

    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_912"})
    fake.on("GET", f"{PAGE}_912", {})
    late = Harness(http, options={"native_schedule": True}, scheduled_at=NOW - timedelta(minutes=1))
    result, _ = late.run()
    assert result.status == "published" and not late.state.get("native_scheduled")
    assert "published" not in fake.find("POST", f"{PAGE}/feed")[0]
    assert any(e[0] == "fb_native_skipped" for e in late.events)


def test_native_reconcile_uses_scheduled_posts(fake: FakeMeta, http):
    when = NOW + timedelta(days=1)
    fake.on("POST", f"{PAGE}/feed", timeout)
    fake.on(
        "GET",
        f"{PAGE}/scheduled_posts",
        {"data": [{"id": f"{PAGE}_913", "message": "Hola  mundo", "scheduled_publish_time": 1}]},
    )
    h = Harness(http, options={"native_schedule": True}, scheduled_at=when)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == f"{PAGE}_913"
    assert h.state["native_scheduled"] is True


def test_delete_post_best_effort(fake: FakeMeta, http):
    account = AccountCreds(7, "facebook", PAGE, "P", "page", "PT", None, None, None, {})
    fake.on("DELETE", f"{PAGE}_1", {"success": True})
    fake.on("DELETE", f"{PAGE}_2", error(100, 33))
    fake.on("DELETE", f"{PAGE}_3", error(1, message="temporary"))
    fake.on("GET", PAGE, error(10))  # the Page itself is not readable: 100/33 may mean lost access
    provider = FacebookProvider()
    assert provider.delete_post(account, SETTINGS, http, f"{PAGE}_1") is True
    assert provider.delete_post(account, SETTINGS, http, f"{PAGE}_2") is False
    assert provider.delete_post(account, SETTINGS, http, f"{PAGE}_3") is False


def test_delete_post_already_deleted_by_the_user_counts_as_withdrawn(fake: FakeMeta, http):
    """The user deleted the scheduled post on Facebook first: DELETE answers 100/33 while the Page is still
    readable with the same token, so the post is gone and a cancel in the app can go ahead."""
    account = AccountCreds(7, "facebook", PAGE, "P", "page", "PT", None, None, None, {})
    fake.on("DELETE", f"{PAGE}_2", error(100, 33))
    fake.on("GET", PAGE, {"id": PAGE})
    assert FacebookProvider().delete_post(account, SETTINGS, http, f"{PAGE}_2") is True
    assert fake.find("GET", PAGE)


# --------------------------------------------------------------------------------------------- video


def test_video_post_chunked_upload_flow(fake: FakeMeta, http, tmp_path):
    video = media(tmp_path, "clip.mp4", "video", size=2560)

    def videos(request, form):
        phase = form.get("upload_phase")
        if phase == "start":
            assert form["file_size"] == "2560"
            return {"video_id": "V1", "upload_session_id": "S1", "start_offset": "0", "end_offset": "1024"}
        if phase == "transfer":
            start = int(form["start_offset"])
            assert form["video_file_chunk"] == video.path.read_bytes()[start : start + 1024][: 2560 - start]
            nxt = min(start + 1024, 2560)
            end = min(nxt + 1024, 2560)
            return {"start_offset": str(nxt), "end_offset": str(end)}
        assert phase == "finish"
        assert form["description"] == "Mi vídeo" and form["title"] == "Título"
        return {"success": True}

    fake.on("POST", f"{PAGE}/videos", videos)
    fake.on(
        "GET",
        "V1",
        {"status": {"video_status": "processing", "publishing_phase": {"status": "in_progress"}}},
        {
            "status": {"video_status": "ready", "publishing_phase": {"status": "complete"}},
            "post_id": "777",
            "permalink_url": "/111/videos/V1",
        },
    )
    h = Harness(http, text="Mi vídeo", items=[video], options={"title": "Título"})
    result, steps = h.run()
    assert [s.status for s in steps] == [
        "continue",
        "continue",
        "continue",
        "continue",
        "wait",
        "wait",
        "published",
    ]
    assert result.external_id == f"{PAGE}_777"
    assert result.external_url == "https://www.facebook.com/111/videos/V1"
    phases = [f["upload_phase"] for f in fake.find("POST", f"{PAGE}/videos")]
    assert phases == ["start", "transfer", "transfer", "transfer", "finish"]
    assert len(h.commits) == 1 and h.commits[0]["commit_stage"] == "finish"


def test_video_transfer_failure_resumes_same_chunk(fake: FakeMeta, http, tmp_path):
    video = media(tmp_path, "clip.mp4", "video", size=1024)
    fake.on(
        "POST",
        f"{PAGE}/videos",
        {"video_id": "V2", "upload_session_id": "S2", "start_offset": "0", "end_offset": "1024"},
        error(1, status=500),
        {"start_offset": "1024", "end_offset": "1024"},
        {"success": True},
    )
    fake.on("GET", "V2", {"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}})
    h = Harness(http, items=[video])
    h.step()
    with pytest.raises(ProviderError):
        h.step()
    assert h.state["transfer_failures"] == 1 and h.state["start_offset"] == 0
    result, _ = h.resumed(http).run()
    assert result.status == "published"
    transfers = [f for f in fake.find("POST", f"{PAGE}/videos") if f.get("upload_phase") == "transfer"]
    assert [t["start_offset"] for t in transfers] == ["0", "0"]


def test_video_falls_back_to_single_upload(fake: FakeMeta, http, tmp_path):
    video = media(tmp_path, "clip.mp4", "video")

    def videos(request, form):
        if form.get("upload_phase") == "start":
            return error(100, None, "(#100) Invalid parameter upload_phase")
        assert form["source"] == video.path.read_bytes()
        return {"id": "V3"}

    fake.on("POST", f"{PAGE}/videos", videos)
    fake.on("GET", "V3", {"status": {"video_status": "ready"}, "published": True})
    h = Harness(http, items=[video])
    result, _ = h.run()
    assert result.external_id == "V3"
    assert h.state["video_mode"] == "simple"


def test_video_processing_error_is_permanent(fake: FakeMeta, http, tmp_path):
    fake.on(
        "POST",
        f"{PAGE}/videos",
        {"video_id": "V4", "upload_session_id": "S", "start_offset": "0", "end_offset": "0"},
        {"success": True},
    )
    fake.on("GET", "V4", {"status": {"video_status": "error", "processing_phase": {"status": "error"}}})
    h = Harness(http, items=[media(tmp_path, "c.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.code == "fb_video_processing_failed"
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "not_published"  # Facebook will never publish it
    assert h.state["stage"] == "start" and "video_id" not in h.state  # a retry uploads again


def test_video_publishing_poll_gives_up_gracefully(fake: FakeMeta, http, tmp_path):
    fake.on(
        "POST",
        f"{PAGE}/videos",
        {"video_id": "V5", "upload_session_id": "S", "start_offset": "0", "end_offset": "0"},
        {"success": True},
    )
    fake.on(
        "GET", "V5", {"status": {"video_status": "processing", "publishing_phase": {"status": "in_progress"}}}
    )
    result, steps = Harness(http, items=[media(tmp_path, "c.mp4", "video")]).run(max_steps=400)
    assert result.status == "published" and "procesándolo" in (result.message or "")


# ---------------------------------------------------------------------------------------------- reels


def _reel_routes(fake: FakeMeta, video_id: str = "R1") -> None:
    def reels(request, form):
        if form["upload_phase"] == "start":
            return {
                "video_id": video_id,
                "upload_url": f"https://rupload.facebook.com/video-upload/v25.0/{video_id}",
            }
        assert form["upload_phase"] == "finish" and form["video_state"] == "PUBLISHED"
        return {"success": True, "post_id": "4444"}

    fake.on("POST", f"{PAGE}/video_reels", reels)
    fake.on("POST", f"/video-upload/v25.0/{video_id}", {"success": True})


def test_reel_flow_start_upload_finish_publish(fake: FakeMeta, http, tmp_path):
    _reel_routes(fake)
    fake.on(
        "GET",
        "R1",
        {"status": {"uploading_phase": {"status": "in_progress"}, "video_status": "uploading"}},
        {"status": {"uploading_phase": {"status": "complete"}, "video_status": "processing"}},
        {
            "status": {"publishing_phase": {"status": "complete"}, "video_status": "ready"},
            "permalink_url": "/reel/R1",
        },
    )
    video = media(tmp_path, "reel.mp4", "video")
    h = Harness(http, format="reel", text="Mi reel", items=[video], options={"title": "T"})
    result, steps = h.run()
    assert result.external_id == f"{PAGE}_4444"
    assert result.external_url == "https://www.facebook.com/reel/R1"
    upload = [c for c in fake.calls if c[1] == "/video-upload/v25.0/R1"][0][3]
    assert upload.headers["authorization"] == "OAuth PAGETOKEN"
    assert upload.headers["file_size"] == str(video.size_bytes)
    assert upload.content == video.path.read_bytes()
    finish = [f for f in fake.find("POST", f"{PAGE}/video_reels") if f["upload_phase"] == "finish"][0]
    assert finish["description"] == "Mi reel" and finish["title"] == "T" and finish["video_id"] == "R1"
    assert [s.status for s in steps].count("wait") >= 2
    assert len(h.commits) == 1 and h.commits[0]["uploaded"] is True


def test_reel_upload_failure_twice_restarts_session(fake: FakeMeta, http, tmp_path):
    fake.on(
        "POST",
        f"{PAGE}/video_reels",
        {"video_id": "R2", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/R2"},
    )
    fake.on("POST", "/video-upload/v25.0/R2", error(1, status=503))
    h = Harness(http, format="reel", items=[media(tmp_path, "r.mp4", "video")])
    h.step()
    for _ in range(2):
        with pytest.raises(ProviderError) as exc:
            h.step()
        assert exc.value.kind == ErrorKind.TRANSIENT
    assert h.state["stage"] == "start" and "video_id" not in h.state


def test_reel_bad_upload_url_is_refused(fake: FakeMeta, http, tmp_path):
    fake.on(
        "POST",
        f"{PAGE}/video_reels",
        {"video_id": "R3", "upload_url": "https://evil.example/video-upload/R3"},
    )
    h = Harness(http, format="reel", items=[media(tmp_path, "r.mp4", "video")])
    h.step()
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.code == "meta_bad_upload_url"
    assert not [c for c in fake.calls if "evil" in str(c[3].url)]


def test_reel_requires_one_video(http, tmp_path):
    with pytest.raises(ProviderError) as exc:
        Harness(http, format="reel", items=[media(tmp_path, "a.jpg")]).step()
    assert exc.value.code == "fb_reel_media"


def test_reel_ambiguous_finish_then_reconcile(fake: FakeMeta, http, tmp_path):
    def reels(request, form):
        if form["upload_phase"] == "start":
            return {"video_id": "R4", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/R4"}
        return httpx.ReadTimeout("timeout", request=request)

    fake.on("POST", f"{PAGE}/video_reels", reels)
    fake.on("POST", "/video-upload/v25.0/R4", {"success": True})
    fake.on(
        "GET",
        "R4",
        {"status": {"uploading_phase": {"status": "complete"}}},
        {"status": {"publishing_phase": {"status": "in_progress"}}, "post_id": "88"},
    )
    h = Harness(http, format="reel", items=[media(tmp_path, "r.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == f"{PAGE}_88"
    # A later publish_step returns the recorded result without sending anything.
    before = len(fake.calls)
    assert h.step().status == "published" and len(fake.calls) == before


def test_reel_reconcile_not_published_allows_safe_retry(fake: FakeMeta, http, tmp_path):
    calls = {"finish": 0}

    def reels(request, form):
        if form["upload_phase"] == "start":
            return {"video_id": "R5", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/R5"}
        calls["finish"] += 1
        if calls["finish"] == 1:
            return httpx.Response(502, text="Bad gateway")
        return {"success": True}

    fake.on("POST", f"{PAGE}/video_reels", reels)
    fake.on("POST", "/video-upload/v25.0/R5", {"success": True})
    fake.on(
        "GET",
        "R5",
        {"status": {"uploading_phase": {"status": "complete"}}},
        {
            "status": {
                "uploading_phase": {"status": "complete"},
                "publishing_phase": {"status": "not_started"},
            }
        },
        {"status": {"publishing_phase": {"status": "complete"}}, "permalink_url": "/reel/R5"},
    )
    h = Harness(http, format="reel", items=[media(tmp_path, "r.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "not_published"
    assert h.state["stage"] == "finish"
    result, _ = h.run()
    assert result.status == "published" and calls["finish"] == 2


# ---------------------------------------------------------------------------------------------- stories


def test_photo_story(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "PH1"})
    fake.on("POST", f"{PAGE}/photo_stories", {"success": True, "post_id": "321"})
    h = Harness(http, format="story", text="", items=[media(tmp_path, "s.jpg")])
    result, steps = h.run()
    assert result.external_id == f"{PAGE}_321"
    assert fake.find("POST", f"{PAGE}/photos")[0]["published"] == "false"
    assert fake.find("POST", f"{PAGE}/photo_stories")[0]["photo_id"] == "PH1"
    assert len(h.commits) == 1


def test_video_story(fake: FakeMeta, http, tmp_path):
    def stories(request, form):
        if form["upload_phase"] == "start":
            return {"video_id": "VS1", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/VS1"}
        assert form == {**form, "upload_phase": "finish", "video_id": "VS1"}
        assert "description" not in form
        return {"success": True, "post_id": "654"}

    fake.on("POST", f"{PAGE}/video_stories", stories)
    fake.on("POST", "/video-upload/v25.0/VS1", {"success": True})
    fake.on("GET", "VS1", {"status": {"video_status": "upload_complete"}})
    result, _ = Harness(http, format="story", items=[media(tmp_path, "s.mp4", "video")]).run()
    assert result.external_id == f"{PAGE}_654" and "Historia" in (result.message or "")


def test_story_reconcile_via_stories_edge(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/photos", {"id": "PH2"})
    fake.on("POST", f"{PAGE}/photo_stories", error(2, status=500))
    fake.on(
        "GET",
        f"{PAGE}/stories",
        {"data": [{"post_id": "999", "media_id": "PH2", "status": "PUBLISHED"}]},
        {"data": [{"post_id": "1000", "media_id": "OTHER", "status": "PUBLISHED"}]},
    )
    h = Harness(http, format="story", items=[media(tmp_path, "s.jpg")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    snapshot = copy.deepcopy(h.state)
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == f"{PAGE}_999"
    h2 = Harness(http, format="story", items=h.ctx.target.media, state=snapshot)
    assert h2.provider.reconcile(h2.ctx).outcome == "not_published"


# --------------------------------------------------------------------------------- ambiguous / guard


def test_ambiguous_text_commit_reconcile_found(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", timeout)
    fake.on(
        "GET",
        f"{PAGE}/feed",
        {"data": [{"id": f"{PAGE}_1", "message": "otra cosa"}, {"id": f"{PAGE}_2", "message": "Hola mundo"}]},
    )
    h = Harness(http)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    assert h.state["stage"] == "committing" and h.commits
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == f"{PAGE}_2"
    since = int(fake.find("GET", f"{PAGE}/feed")[0]["since"])
    assert since == int((NOW - timedelta(minutes=10)).timestamp())


def test_ambiguous_text_commit_reconcile_not_found(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", timeout)
    fake.on("GET", f"{PAGE}/feed", {"data": []})
    h = Harness(http)
    with pytest.raises(ProviderError):
        h.step()
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "not_published"
    assert h.state["stage"] == "commit" and "commit_at" not in h.state


def test_reconcile_unknown_when_lookup_fails_or_no_text(fake: FakeMeta, http, tmp_path):
    fake.on("POST", f"{PAGE}/feed", timeout)
    fake.on("GET", f"{PAGE}/feed", error(2, status=500))
    h = Harness(http)
    with pytest.raises(ProviderError):
        h.step()
    assert h.provider.reconcile(h.ctx).outcome == "unknown"

    fake.on("POST", f"{PAGE}/photos", timeout)
    h2 = Harness(http, text="", items=[media(tmp_path, "a.jpg")])
    with pytest.raises(ProviderError):
        h2.step()
    assert h2.provider.reconcile(h2.ctx).outcome == "unknown"
    assert Harness(http).provider.reconcile(Harness(http).ctx).outcome == "unknown"  # no commit recorded


def test_crash_after_before_commit_rechecks_before_resending(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", timeout, {"id": f"{PAGE}_3"})
    fake.on(
        "GET", f"{PAGE}/feed", {"data": [{"id": f"{PAGE}_3", "message": "Hola mundo", "permalink_url": "u"}]}
    )
    h = Harness(http)
    with pytest.raises(ProviderError):
        h.step()
    resumed = Harness(http, state=copy.deepcopy(h.commits[-1]))
    result = resumed.step()
    assert result.status == "published" and result.external_id == f"{PAGE}_3"
    assert len(fake.find("POST", f"{PAGE}/feed")) == 1  # found remotely: not re-sent


def test_guard_raises_ambiguous_when_remote_state_unknown(fake: FakeMeta, http):
    fake.on("GET", f"{PAGE}/feed", error(2, status=500))
    state = {
        "v": 1,
        "flow": "text",
        "stage": "committing",
        "commit_stage": "commit",
        "commit_at": NOW.isoformat(),
    }
    h = Harness(http, state=state)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.TRANSIENT  # lookup failed; engine will reconcile (commit_sent)


# ------------------------------------------------------------------------------------- errors / auth


def test_auth_error_before_commit(fake: FakeMeta, http):
    fake.on("POST", f"{PAGE}/feed", error(190, 463, "Session has expired"))
    h = Harness(http)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.AUTH and exc.value.code == "meta_token_expired"
    assert h.state["commit_rejected"] is True


def test_missing_token_is_auth(http):
    with pytest.raises(ProviderError) as exc:
        Harness(http, token=None).step()
    assert exc.value.kind == ErrorKind.AUTH


def test_throttling_with_retry_after(fake: FakeMeta, http):
    headers = {
        "x-business-use-case-usage": json.dumps(
            {PAGE: [{"type": "pages", "call_count": 100, "estimated_time_to_regain_access": 12}]}
        )
    }
    fake.on(
        "POST",
        f"{PAGE}/feed",
        httpx.Response(400, json={"error": {"code": 80001, "message": "too many"}}, headers=headers),
    )
    h = Harness(http)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.RATE_LIMITED and exc.value.retry_after == 720
    assert h.provider.reconcile(h.ctx).outcome == "not_published"


def test_http_200_with_error_body_at_commit_is_rejected(fake: FakeMeta, http):
    fake.on(
        "POST", f"{PAGE}/feed", httpx.Response(200, json={"error": {"code": 506, "message": "Duplicate"}})
    )
    h = Harness(http)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.code == "fb_duplicate"


def test_check_account_and_refresh(fake: FakeMeta, http):
    account = AccountCreds(7, "facebook", PAGE, "P", "page", "PT", None, None, None, {})
    fake.on("GET", PAGE, {"id": PAGE, "name": "P"}, error(190, 460))
    provider = FacebookProvider()
    provider.check_account(account, SETTINGS, http)
    with pytest.raises(ProviderError) as exc:
        provider.check_account(account, SETTINGS, http)
    assert exc.value.kind == ErrorKind.AUTH
    assert provider.refresh_credentials(account, SETTINGS, http, ManualClock(NOW)) is None


# ----------------------------------------------------------------------------------------- analytics


def _account() -> AccountCreds:
    return AccountCreds(7, "facebook", PAGE, "Mi Página", "page", "PT", None, None, None, {})


def test_account_snapshot_with_metric_fallback(fake: FakeMeta, http):
    day = date(2026, 11, 2)

    def insights(request, form):
        metric = form["metric"]
        assert form["period"] == "day"
        if "," in metric or metric == "page_post_engagements":
            return error(100, None, "(#100) The value must be a valid insights metric")
        values = {"page_media_view": 1500, "page_total_media_view_unique": 900}
        return {
            "data": [
                {
                    "name": metric,
                    "period": "day",
                    "values": [{"value": values[metric], "end_time": "2026-11-03T08:00:00+0000"}],
                }
            ]
        }

    fake.on("GET", PAGE, {"followers_count": 2500, "fan_count": 2400})
    fake.on("GET", f"{PAGE}/insights", insights)
    snap = FacebookProvider().fetch_account_snapshot(_account(), SETTINGS, http, day)
    assert snap.day == day
    assert snap.metrics == {"followers": 2500, "views": 1500, "reach": 900}


def test_account_snapshot_picks_the_requested_day_from_the_series(fake: FakeMeta, http):
    """The two-UTC-day window returns the previous Pacific day too: only the value closing ``day`` is kept,
    and a series without it stores no flow metric rather than a neighbouring day's number."""
    day = date(2026, 11, 2)
    seen: list[dict] = []

    previous = {"value": 111, "end_time": "2026-11-02T08:00:00+0000"}
    series = {
        "page_media_view": [previous, {"value": 222, "end_time": "2026-11-03T08:00:00+0000"}],
        "page_total_media_view_unique": [previous],  # Meta has not closed ``day`` for this one yet
        "page_post_engagements": [{"value": 9, "end_time": "2026-11-03T08:00:00+0000"}],
    }

    def insights(request, form):
        seen.append(form)
        names = form["metric"].split(",")
        return {"data": [{"name": m, "period": "day", "values": series[m]} for m in names]}

    fake.on("GET", PAGE, {"followers_count": 10})
    fake.on("GET", f"{PAGE}/insights", insights)
    snap = FacebookProvider().fetch_account_snapshot(_account(), SETTINGS, http, day)
    assert int(seen[0]["until"]) - int(seen[0]["since"]) == 2 * 86400
    assert snap.metrics == {"followers": 10, "views": 222, "interactions": 9}


def test_account_snapshot_survives_missing_insights_permission(fake: FakeMeta, http):
    fake.on("GET", PAGE, {"fan_count": 10})
    fake.on("GET", f"{PAGE}/insights", error(200, None, "(#200) Requires read_insights"))
    snap = FacebookProvider().fetch_account_snapshot(_account(), SETTINGS, http, date(2026, 11, 2))
    assert snap.metrics == {"followers": 10}


def test_background_jobs_pause_on_high_usage(fake: FakeMeta, http):
    fake.on(
        "GET",
        PAGE,
        httpx.Response(200, json={"id": PAGE}, headers={"x-app-usage": json.dumps({"call_count": 95})}),
    )
    provider = FacebookProvider()
    provider.check_account(_account(), SETTINGS, http)
    with pytest.raises(ProviderError) as exc:
        provider.fetch_account_snapshot(_account(), SETTINGS, http, date(2026, 11, 2))
    assert exc.value.kind == ErrorKind.RATE_LIMITED


def test_recent_posts_metrics(fake: FakeMeta, http):
    since = NOW - timedelta(days=30)
    fake.on(
        "GET",
        f"{PAGE}/posts",
        {
            "data": [
                {
                    "id": f"{PAGE}_10",
                    "message": "Hola",
                    "created_time": "2026-11-01T10:00:00+0000",
                    "permalink_url": "https://fb/10",
                    "full_picture": "https://cdn/10.jpg",
                    "status_type": "added_photos",
                    "shares": {"count": 3},
                    "reactions": {"summary": {"total_count": 20}},
                    "comments": {"summary": {"total_count": 5}},
                },
                {"id": f"{PAGE}_11", "created_time": "2026-09-01T10:00:00+0000"},
            ]
        },
    )
    fake.on(
        "GET",
        f"{PAGE}_10/insights",
        {
            "data": [
                {"name": "post_media_view", "values": [{"value": 1000}]},
                {"name": "post_total_media_view_unique", "values": [{"value": 560}]},
            ]
        },
    )
    posts = FacebookProvider().fetch_recent_posts(_account(), SETTINGS, http, since)
    assert len(posts) == 1
    post = posts[0]
    assert post.external_id == f"{PAGE}_10" and post.media_type == "IMAGE"
    assert post.published_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert post.metrics == {
        "likes": 20,
        "comments": 5,
        "shares": 3,
        "views": 1000,
        "reach": 560,
        "interactions": 28,
        "engagement": 5.0,
    }


def test_recent_posts_token_error_propagates(fake: FakeMeta, http):
    fake.on("GET", f"{PAGE}/posts", error(190, 467))
    with pytest.raises(ProviderError) as exc:
        FacebookProvider().fetch_recent_posts(_account(), SETTINGS, http, NOW - timedelta(days=30))
    assert exc.value.kind == ErrorKind.AUTH


# ------------------------------------------------------------------------------------------ comments


def test_fetch_comments_flattens_and_marks_own(fake: FakeMeta, http):
    since = NOW - timedelta(days=2)

    def feed(request, form):
        assert "filter(stream)" in form["fields"]
        return {
            "data": [
                {
                    "id": f"{PAGE}_20",
                    "permalink_url": "https://fb/20",
                    "comments": {
                        "data": [
                            {
                                "id": "20_1",
                                "message": "¡Genial!",
                                "created_time": "2026-11-02T10:00:00+0000",
                                "from": {"id": "u1", "name": "Ana"},
                            },
                            {
                                "id": "20_2",
                                "message": "Gracias Ana",
                                "created_time": "2026-11-02T11:00:00+0000",
                                "from": {"id": PAGE, "name": "Mi Página"},
                                "parent": {"id": "20_1"},
                            },
                            {"id": "20_0", "message": "viejo", "created_time": "2026-10-01T11:00:00+0000"},
                        ],
                        "paging": {"next": f"https://graph.facebook.com/v25.0/{PAGE}_20/comments?after=X"},
                    },
                }
            ]
        }

    fake.on("GET", f"{PAGE}/feed", feed)
    fake.on(
        "GET",
        f"{PAGE}_20/comments",
        {
            "data": [
                {
                    "id": "20_3",
                    "message": "más",
                    "created_time": "2026-11-02T12:00:00+0000",
                    "from": {"id": "u2"},
                }
            ]
        },
    )
    comments = FacebookProvider().fetch_comments(_account(), SETTINGS, http, since)
    assert [c.external_id for c in comments] == ["20_1", "20_2", "20_3"]
    first, reply, third = comments
    assert first.author_name == "Ana" and not first.from_us and first.parent_external_id is None
    assert first.object_external_id == f"{PAGE}_20" and first.object_permalink == "https://fb/20"
    assert reply.from_us and reply.parent_external_id == "20_1"
    assert third.text == "más"
    assert fake.find("GET", f"{PAGE}_20/comments")[0]["access_token"] == "PT"


def test_reply_comment_goes_to_parent_for_nested_replies(fake: FakeMeta, http):
    fake.on("GET", "20_2", {"id": "20_2", "parent": {"id": "20_1"}})
    fake.on("GET", "20_1", {"id": "20_1"})
    fake.on("POST", "20_1/comments", {"id": "20_9"})
    provider = FacebookProvider()
    assert provider.reply_comment(_account(), SETTINGS, http, "20_2", "¡Gracias!") == "20_9"
    assert provider.reply_comment(_account(), SETTINGS, http, "20_1", "Hola") == "20_9"
    assert [f["message"] for f in fake.find("POST", "20_1/comments")] == ["¡Gracias!", "Hola"]
    with pytest.raises(ProviderError):
        provider.reply_comment(_account(), SETTINGS, http, "20_1", "  ")


def test_provider_metadata():
    provider = FacebookProvider()
    assert provider.network == "facebook" and provider.supports_comments
    assert [f.key for f in provider.formats] == ["post", "reel", "story"]
    assert provider.format_spec("reel").media == "video"
    assert all(callable(getattr(provider, n)) for n in ("publish_step", "reconcile", "fetch_comments"))


# ------------------------------------------------------------------------------------ extra paths


def test_native_scheduled_video_returns_immediately(fake: FakeMeta, http, tmp_path):
    when = NOW + timedelta(days=3)
    fake.on(
        "POST",
        f"{PAGE}/videos",
        {"video_id": "VN", "upload_session_id": "S", "start_offset": "0", "end_offset": "0"},
        {"success": True},
    )
    h = Harness(
        http, items=[media(tmp_path, "c.mp4", "video")], options={"native_schedule": True}, scheduled_at=when
    )
    result, _ = h.run()
    assert result.external_id == "VN" and h.state["native_scheduled"] is True
    finish = fake.find("POST", f"{PAGE}/videos")[-1]
    assert finish["upload_phase"] == "finish" and finish["published"] == "false"
    assert finish["scheduled_publish_time"] == str(int(when.timestamp()))
    assert not fake.find("GET", "VN")  # no publishing poll for a scheduled video


def test_simple_video_upload_ambiguous_reconciles_by_description(fake: FakeMeta, http, tmp_path):
    def videos(request, form):
        if form.get("upload_phase") == "start":
            return error(100, None, "(#100) Invalid parameter")
        return httpx.ReadTimeout("timeout", request=request)

    fake.on("POST", f"{PAGE}/videos", videos)
    fake.on(
        "GET",
        f"{PAGE}/videos",
        {"data": [{"id": "VX", "description": "Mi vídeo", "created_time": "2026-11-03T09:00:30+0000"}]},
        {"data": [{"id": "VOLD", "description": "Mi vídeo", "created_time": "2026-10-01T09:00:30+0000"}]},
    )
    h = Harness(http, text="Mi vídeo", items=[media(tmp_path, "c.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    snapshot = copy.deepcopy(h.state)
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == "VX"
    h2 = Harness(http, text="Mi vídeo", items=h.ctx.target.media, state=snapshot)
    assert h2.provider.reconcile(h2.ctx).outcome == "not_published"  # only an old video matches


def test_video_story_reconcile_falls_back_to_video_status(fake: FakeMeta, http, tmp_path):
    def stories(request, form):
        if form["upload_phase"] == "start":
            return {"video_id": "VS2", "upload_url": "https://rupload.facebook.com/video-upload/v25.0/VS2"}
        return httpx.Response(503, text="unavailable")

    fake.on("POST", f"{PAGE}/video_stories", stories)
    fake.on("POST", "/video-upload/v25.0/VS2", {"success": True})
    fake.on(
        "GET",
        "VS2",
        {"status": {"video_status": "upload_complete"}},
        {"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}, "post_id": "55"},
    )
    fake.on("GET", f"{PAGE}/stories", {"data": [{"post_id": "1", "status": "PUBLISHED"}]})
    h = Harness(http, format="story", items=[media(tmp_path, "s.mp4", "video")])
    with pytest.raises(ProviderError) as exc:
        h.run()
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    rec = h.provider.reconcile(h.ctx)
    assert rec.outcome == "published" and rec.external_id == f"{PAGE}_55"


def test_story_requires_single_media(http, tmp_path):
    with pytest.raises(ProviderError) as exc:
        Harness(
            http, format="story", items=[media(tmp_path, "a.jpg"), media(tmp_path, "b.jpg", index=2)]
        ).step()
    assert exc.value.code == "fb_story_media"


def test_guard_rechecks_then_recommits_when_not_published(fake: FakeMeta, http):
    fake.on("GET", f"{PAGE}/feed", {"data": []})
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_77"})
    fake.on("GET", f"{PAGE}_77", {})
    state = {
        "v": 1,
        "flow": "text",
        "stage": "committing",
        "commit_stage": "commit",
        "commit_at": NOW.isoformat(),
    }
    h = Harness(http, state=state)
    result = h.step()
    assert result.external_id == f"{PAGE}_77" and len(h.commits) == 1


def test_guard_unknown_raises_ambiguous_without_resending(fake: FakeMeta, http, tmp_path):
    state = {
        "v": 1,
        "flow": "photo_single",
        "stage": "committing",
        "commit_stage": "commit",
        "commit_at": NOW.isoformat(),
        "sent": {"message": ""},
    }
    h = Harness(http, text="", items=[media(tmp_path, "a.jpg")], state=state)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "fb_commit_unknown"
    assert not fake.calls


# ------------------------------------------------------------------- integration: early claims and cancel


def test_non_native_post_claimed_early_waits_until_its_time(http):
    """The engine claims ``native_schedule`` targets ahead of time; a format Facebook cannot schedule (or the
    option off) must never be published before ``scheduled_at``."""
    reel = Harness(
        http, format="reel", options={"native_schedule": True}, scheduled_at=NOW + timedelta(hours=2)
    )
    result = reel.step()
    assert result.status == "wait" and result.wait_seconds == 7200
    assert not reel.commits and "flow" not in reel.state

    plain = Harness(http, options={}, scheduled_at=NOW + timedelta(minutes=3))
    result = plain.step()
    assert result.status == "wait" and result.wait_seconds == 180


def test_cancel_native_deletes_the_scheduled_post(fake: FakeMeta, http):
    when = NOW + timedelta(days=2)
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_920"})
    fake.on("GET", f"{PAGE}_920", {})
    fake.on("DELETE", f"{PAGE}_920", {"success": True})
    h = Harness(http, options={"native_schedule": True}, scheduled_at=when)
    h.run()
    h.provider.cancel_native(h.ctx)
    assert fake.find("DELETE", f"{PAGE}_920")
    assert h.state["native_scheduled"] is False and h.state["native_cancelled"] is True


def test_cancel_native_failures_raise_permanent(fake: FakeMeta, http):
    h = Harness(http, state={"native_scheduled": True})
    with pytest.raises(ProviderError) as exc:
        h.provider.cancel_native(h.ctx)
    assert exc.value.code == "fb_native_no_id"
    fake.on("DELETE", f"{PAGE}_921", error(100, 33))
    fake.on("GET", PAGE, error(10))
    h = Harness(http, state={"native_scheduled": True, "result": {"external_id": f"{PAGE}_921"}})
    with pytest.raises(ProviderError) as exc:
        h.provider.cancel_native(h.ctx)
    assert exc.value.kind == ErrorKind.PERMANENT


# ------------------------------------------------------------------- review regressions (meta-conformance)


def test_native_min_matches_the_rules_window(fake: FakeMeta, http):
    from planificador import rules
    from planificador.providers import facebook

    assert facebook.NATIVE_MIN == rules.FB_NATIVE_WINDOW[0]
    assert facebook.NATIVE_COMMIT_MIN >= timedelta(minutes=10)  # Meta's own minimum (§3.7)
    soon = Harness(http, options={"native_schedule": True}, scheduled_at=NOW + timedelta(minutes=14))
    result = soon.step()
    assert result.status == "wait" and "flow" not in soon.state  # told "se publicará desde la app"


def test_native_photos_lead_too_short_after_retry_publish_directly_at_time(fake: FakeMeta, http, tmp_path):
    """Repro: 2 photos scheduled 16 min ahead; photo 2 times out and the engine retries 5 min later. The
    stored scheduled_publish_time would be < 12 min ahead (Meta wants ≥ 10): native is dropped, the temporary
    photos are restaged and the post goes out directly at its time instead of failing."""
    when = NOW + timedelta(minutes=16)
    calls = {"n": 0}

    def photos(request, form):
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.ReadTimeout("t", request=request)
        return {"id": f"p{calls['n']}"}

    fake.on("POST", f"{PAGE}/photos", photos)
    fake.on("DELETE", "p1", {"success": True})
    fake.on("POST", f"{PAGE}/feed", {"id": f"{PAGE}_1"})
    fake.on("GET", f"{PAGE}_1", {})
    h = Harness(
        http,
        items=[media(tmp_path, "a.jpg"), media(tmp_path, "b.jpg", index=2)],
        options={"native_schedule": True},
        scheduled_at=when,
    )
    assert h.step().status == "continue" and h.state["native"] is True
    with pytest.raises(ProviderError):
        h.step()
    h.clock.advance(seconds=5 * 60)
    result, _ = h.run()
    assert result.status == "published" and not h.state.get("native_scheduled")
    assert any(e[0] == "fb_native_dropped" for e in h.events)
    assert fake.find("DELETE", "p1")  # the temporary photo was discarded
    uploads = fake.find("POST", f"{PAGE}/photos")
    assert [u.get("temporary") for u in uploads] == ["true", "true", None, None]
    feed = fake.find("POST", f"{PAGE}/feed")
    assert len(feed) == 1 and "scheduled_publish_time" not in feed[0] and "published" not in feed[0]
    assert h.clock.now() >= when  # never before its time
    assert len(h.commits) == 1 and mc.parse_time(h.commits[0]["commit_at"]) >= when


def test_native_text_lead_rechecked_at_each_commit_attempt(fake: FakeMeta, http):
    when = NOW + timedelta(minutes=16)
    fake.on(
        "POST",
        f"{PAGE}/feed",
        lambda request, form: httpx.ConnectError("down", request=request),
        {"id": f"{PAGE}_2"},
    )
    fake.on("GET", f"{PAGE}_2", {})
    h = Harness(http, options={"native_schedule": True}, scheduled_at=when)
    with pytest.raises(ProviderError) as exc:
        h.step()
    assert exc.value.kind == ErrorKind.TRANSIENT and h.state.get("commit_rejected")
    assert fake.find("POST", f"{PAGE}/feed")[0]["scheduled_publish_time"] == str(int(when.timestamp()))
    h.clock.advance(seconds=6 * 60)  # engine retry: the lead is now 10 min
    held = h.step()
    assert held.status == "wait" and held.wait_seconds == 10 * 60
    assert len(fake.find("POST", f"{PAGE}/feed")) == 1
    h.clock.advance(seconds=held.wait_seconds)
    result = h.step()
    assert result.status == "published" and "Programado" not in (result.message or "")
    assert "scheduled_publish_time" not in fake.find("POST", f"{PAGE}/feed")[1]


def test_native_single_photo_drops_to_direct_photo_post(fake: FakeMeta, http, tmp_path):
    when = NOW + timedelta(minutes=11)
    fake.on("POST", f"{PAGE}/photos", {"id": "p9", "post_id": f"{PAGE}_9"})
    fake.on("GET", f"{PAGE}_9", {})
    h = Harness(
        http,
        items=[media(tmp_path, "a.jpg")],
        options={"native_schedule": True},
        scheduled_at=when,
        state={
            "v": 1,
            "flow": "photos",
            "stage": "start",
            "native": True,
            "native_time": int(when.timestamp()),
        },
    )
    result, _ = h.run()
    assert result.status == "published" and h.state["flow"] == "photo_single"
    upload = fake.find("POST", f"{PAGE}/photos")[0]
    assert upload["published"] == "true" and "temporary" not in upload
    assert h.clock.now() >= when


def test_native_video_finish_after_slow_upload_publishes_directly(fake: FakeMeta, http, tmp_path):
    when = NOW + timedelta(minutes=20)
    video = media(tmp_path, "clip.mp4", "video", size=2048)
    clock = ManualClock(NOW)

    def videos(request, form):
        phase = form.get("upload_phase")
        if phase == "start":
            return {"video_id": "V7", "upload_session_id": "S7", "start_offset": "0", "end_offset": "1024"}
        if phase == "transfer":
            clock.advance(seconds=5 * 60)  # a slow connection
            start = int(form["start_offset"]) + 1024
            return {"start_offset": str(start), "end_offset": str(min(start + 1024, 2048))}
        return {"success": True}

    fake.on("POST", f"{PAGE}/videos", videos)
    fake.on("GET", "V7", {"status": {"video_status": "ready", "publishing_phase": {"status": "complete"}}})
    h = Harness(http, items=[video], options={"native_schedule": True}, scheduled_at=when, clock=clock)
    result, _ = h.run()
    assert result.status == "published" and not h.state.get("native_scheduled")
    finish = [f for f in fake.find("POST", f"{PAGE}/videos") if f.get("upload_phase") == "finish"]
    assert len(finish) == 1 and "scheduled_publish_time" not in finish[0]
    assert h.clock.now() >= when


def test_native_video_with_enough_lead_stays_native(fake: FakeMeta, http, tmp_path):
    when = NOW + timedelta(minutes=40)
    fake.on(
        "POST",
        f"{PAGE}/videos",
        {"video_id": "VN2", "upload_session_id": "S", "start_offset": "0", "end_offset": "0"},
        {"success": True},
    )
    h = Harness(
        http, items=[media(tmp_path, "c.mp4", "video")], options={"native_schedule": True}, scheduled_at=when
    )
    result, _ = h.run()
    assert result.status == "published" and h.state["native_scheduled"] is True
    assert fake.find("POST", f"{PAGE}/videos")[-1]["scheduled_publish_time"] == str(int(when.timestamp()))

"""Tests for the shared Meta Graph helpers (providers/meta_common.py): requests, error classification, usage
headers, OAuth helpers, pagination, rupload, insights and image preparation. Fully offline (respx)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
    TargetSpec,
)
from planificador.timeutil import ManualClock

G = "https://graph.facebook.com/v25.0"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def settings(**overrides) -> ProviderSettings:
    values = dict(
        meta_app_id="123456",
        meta_app_secret="app-secret",
        meta_api_version="v25.0",
        tiktok_client_key=None,
        tiktok_client_secret=None,
        tiktok_pkce_encoding="hex",
        base_url="http://localhost:8765",
        demo_mode=False,
    )
    values.update(overrides)
    return ProviderSettings(**values)


@pytest.fixture(autouse=True)
def _reset_usage():
    mc.reset_usage()
    yield
    mc.reset_usage()


@pytest.fixture
def http():
    with respx.mock(assert_all_called=False) as router:
        client = httpx.Client()
        client.router = router  # type: ignore[attr-defined]
        yield client
        client.close()


def err(code: int | None = None, sub: int | None = None, message: str = "boom", **extra) -> dict:
    body = {"message": message, "type": "OAuthException", "fbtrace_id": "TRACE"}
    if code is not None:
        body["code"] = code
    if sub is not None:
        body["error_subcode"] = sub
    body.update(extra)
    return {"error": body}


# --------------------------------------------------------------------------------------------- basics


def test_graph_url_and_version_normalisation():
    assert mc.graph_url(settings(), "123/feed") == f"{G}/123/feed"
    assert mc.graph_url(settings(), "/me/accounts") == f"{G}/me/accounts"
    assert mc.graph_url(settings(meta_api_version="26.0"), "me") == "https://graph.facebook.com/v26.0/me"
    assert mc.graph_url(settings(meta_api_version=""), "me") == f"{G}/me"
    assert mc.graph_url(settings(), "https://example.com/x") == "https://example.com/x"
    assert mc.redirect_uri(settings()) == "http://localhost:8765/conexiones/callback/meta"


def test_auth_params_adds_appsecret_proof_and_requires_token():
    params = mc.auth_params(settings(), "tok")
    assert params["access_token"] == "tok"
    assert params["appsecret_proof"] == mc.appsecret_proof("tok", "app-secret")
    assert "appsecret_proof" not in mc.auth_params(settings(meta_app_secret=None), "tok")
    with pytest.raises(ProviderError) as exc:
        mc.auth_params(settings(), None)
    assert exc.value.kind == ErrorKind.AUTH


def test_form_fields_encoding():
    out = mc.form_fields({"a": True, "b": False, "c": None, "d": ["x", "y"], "e": {"k": 1}, "f": 5})
    assert out == {"a": "true", "b": "false", "d": '["x","y"]', "e": '{"k":1}', "f": "5"}


def test_parse_time_variants():
    assert mc.parse_time("2026-10-15T10:00:00+0000") == datetime(2026, 10, 15, 10, tzinfo=UTC)
    assert mc.parse_time("2026-10-15T10:00:00Z") == datetime(2026, 10, 15, 10, tzinfo=UTC)
    assert mc.parse_time(1760522400) == datetime.fromtimestamp(1760522400, tz=UTC)
    assert mc.parse_time("1760522400") == datetime.fromtimestamp(1760522400, tz=UTC)
    assert mc.parse_time(None) is None
    assert mc.parse_time("nope") is None
    assert mc.absolute_facebook_url("/reel/1") == "https://www.facebook.com/reel/1"
    assert mc.normalize_text("  a \n b  ") == "a b"


# ------------------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("sub", "code"),
    [
        (463, "meta_token_expired"),
        (460, "meta_password_changed"),
        (459, "meta_checkpoint"),
        (458, "meta_app_removed"),
        (492, "meta_page_role_lost"),
        (467, "meta_token_invalid"),
        (None, "meta_token_invalid"),
    ],
)
def test_classify_token_errors_are_auth(sub, code):
    exc = mc.classify_meta_error(400, err(190, sub), {})
    assert exc.kind == ErrorKind.AUTH
    assert exc.code == code
    assert mc.is_token_error(exc)
    assert "conecta" in exc.message.lower()


def test_classify_checkpoint_by_message():
    exc = mc.classify_meta_error(
        400, err(190, None, "You cannot access the app till you log in to www.facebook.com"), {}
    )
    assert exc.code == "meta_checkpoint"


@pytest.mark.parametrize("code", [4, 17, 32, 613, 80000, 80001, 80002, 80004, 80009, 80014])
def test_classify_throttling_codes(code):
    exc = mc.classify_meta_error(400, err(code), {})
    assert exc.kind == ErrorKind.RATE_LIMITED
    assert exc.retry_after == mc.DEFAULT_THROTTLE_WAIT


def test_classify_http_429_and_regain_time_from_buc_header():
    headers = {
        "x-business-use-case-usage": json.dumps(
            {
                "111": [
                    {
                        "type": "pages",
                        "call_count": 100,
                        "total_cputime": 20,
                        "estimated_time_to_regain_access": 7,
                    }
                ]
            }
        )
    }
    exc = mc.classify_meta_error(429, None, headers)
    assert exc.kind == ErrorKind.RATE_LIMITED
    assert exc.retry_after == 7 * 60
    assert exc.detail["usage"]["buc:111:pages"] == 100


def test_classify_blocked_and_quota():
    blocked = mc.classify_meta_error(400, err(368, 1390008), {})
    assert blocked.kind == ErrorKind.RATE_LIMITED and blocked.retry_after == mc.BLOCKED_WAIT
    quota = mc.classify_meta_error(400, err(9, 2207042), {})
    assert quota.kind == ErrorKind.RATE_LIMITED and quota.code == "ig_quota_reached"


@pytest.mark.parametrize(
    ("code", "sub", "kind", "our"),
    [
        (9007, 2207027, ErrorKind.TRANSIENT, "ig_media_not_ready"),
        (9004, 2207052, ErrorKind.TRANSIENT, "ig_media_fetch_failed"),
        (9004, None, ErrorKind.TRANSIENT, "ig_media_fetch_failed"),
        (-1, 2207020, ErrorKind.TRANSIENT, "ig_container_expired"),
        (36003, 2207009, ErrorKind.PERMANENT, "ig_aspect_ratio"),
        (36003, None, ErrorKind.PERMANENT, "ig_aspect_ratio"),
        (-1, 2207026, ErrorKind.PERMANENT, "ig_video_format"),
        (-1, 2207018, ErrorKind.PERMANENT, "ig_collaborators"),
        (-1, 2207078, ErrorKind.PERMANENT, "ig_trial_reel_unavailable"),
        (10, 2207050, ErrorKind.PERMANENT, "ig_user_restricted"),
        (4, 2207051, ErrorKind.PERMANENT, "ig_activity_restricted"),  # Meta sends code 4 (throttling)
        (-1, 2207051, ErrorKind.PERMANENT, "ig_activity_restricted"),
        (-1, 2207001, ErrorKind.TRANSIENT, "ig_server_error"),
        (20, 1772107, ErrorKind.PERMANENT, "ig_comment_failed"),
        (100, 1366046, ErrorKind.PERMANENT, "fb_photo_invalid"),
        (100, 1609005, ErrorKind.PERMANENT, "fb_link_scrape"),
        (368, 1404078, ErrorKind.PERMANENT, "fb_publishing_authorization"),
        (506, None, ErrorKind.PERMANENT, "fb_duplicate"),
    ],
)
def test_classify_error_map(code, sub, kind, our):
    exc = mc.classify_meta_error(400, err(code, sub), {})
    assert exc.kind == kind
    assert exc.code == our
    assert exc.message  # Spanish, user facing


def test_classify_action_block_code_4_is_not_a_plain_rate_limit():
    exc = mc.classify_meta_error(400, err(4, 2207051, "We restrict certain activity"), {})
    assert exc.kind == ErrorKind.PERMANENT and exc.code == "ig_activity_restricted"
    assert exc.retry_after == mc.ACTION_BLOCK_WAIT and "24 horas" in exc.message
    plain = mc.classify_meta_error(400, err(4, None, "Application request limit reached"), {})
    assert plain.kind == ErrorKind.RATE_LIMITED and plain.code == "meta_rate_limited"


def test_classify_2207001_is_a_retryable_server_error():
    exc = mc.classify_meta_error(400, err(-1, 2207001, "Instagram server error", is_transient=True), {})
    assert exc.kind == ErrorKind.TRANSIENT and exc.code == "ig_server_error"
    assert exc.retry_after == mc.IG_SERVER_ERROR_WAIT and "spam" not in exc.message
    assert exc.code not in mc.DEFINITE_TRANSIENT_CODES  # on media_publish → AMBIGUOUS → read the container


def test_classify_message_based_cases():
    story = mc.classify_meta_error(400, err(10, None, "(#10) The user is not an Instagram Business"), {})
    assert story.kind == ErrorKind.PERMANENT and story.code == "ig_not_business"
    wrong = mc.classify_meta_error(
        400, err(100, None, "Unpublished posts must be posted to a page as the page itself."), {}
    )
    assert wrong.kind == ErrorKind.AUTH
    metric = mc.classify_meta_error(
        400, err(100, None, "(#100) The value must be a valid insights metric"), {}
    )
    assert metric.code == "meta_invalid_metric"
    secret = mc.classify_meta_error(400, err(1, None, "Error validating client secret."), {})
    assert secret.code == "meta_bad_app_secret" and secret.kind == ErrorKind.PERMANENT
    redirect = mc.classify_meta_error(
        400,
        err(
            100, 36008, "Error validating verification code. Please make sure your redirect_uri is identical"
        ),
        {},
    )
    assert redirect.code == "meta_redirect_uri"


def test_classify_permissions_transient_and_default():
    perm = mc.classify_meta_error(403, err(200, None, "(#200) Permissions error"), {})
    assert perm.kind == ErrorKind.AUTH and perm.code == "meta_permission"
    assert not mc.is_token_error(perm)
    assert (
        mc.classify_meta_error(500, err(2, None, "Service temporarily unavailable"), {}).kind
        == ErrorKind.TRANSIENT
    )
    assert (
        mc.classify_meta_error(400, err(1, None, "An unknown error occurred"), {}).kind == ErrorKind.TRANSIENT
    )
    assert mc.classify_meta_error(400, err(99, None, "x", is_transient=True), {}).kind == ErrorKind.TRANSIENT
    assert mc.classify_meta_error(502, None, {}).kind == ErrorKind.TRANSIENT
    gone = mc.classify_meta_error(400, err(100, 33, "Object does not exist"), {})
    assert gone.code == "meta_not_found" and gone.kind == ErrorKind.PERMANENT
    other = mc.classify_meta_error(
        400, err(100, None, "Invalid parameter", error_user_msg="Texto no válido"), {}
    )
    assert other.kind == ErrorKind.PERMANENT
    assert other.code == "meta_error_100"
    assert "Texto no válido" in other.message
    assert other.detail["fbtrace_id"] == "TRACE"


def test_classify_rupload_debug_info():
    retry = mc.classify_meta_error(400, {"debug_info": {"retriable": True, "type": "X", "message": "m"}}, {})
    assert retry.kind == ErrorKind.TRANSIENT
    fatal = mc.classify_meta_error(400, {"debug_info": {"retriable": False, "message": "bad file"}}, {})
    assert fatal.kind == ErrorKind.PERMANENT and "bad file" in fatal.message


def test_classify_scrubs_tokens_from_detail():
    exc = mc.classify_meta_error(400, err(100, None, "bad url ?access_token=SECRETTOKEN123&x=1"), {})
    assert "SECRETTOKEN123" not in json.dumps(exc.detail)
    assert "SECRETTOKEN123" not in exc.message


# ------------------------------------------------------------------------------------------- requests


def test_graph_request_http_200_with_error_body_raises(http):
    http.router.get(f"{G}/me").respond(200, json=err(190, 463))
    with pytest.raises(ProviderError) as exc:
        mc.graph_request(http, "GET", f"{G}/me", params={"access_token": "tok"})
    assert exc.value.kind == ErrorKind.AUTH
    assert exc.value.detail["url"] == f"{G}/me"  # no query string (token) in details


def test_graph_request_shapes(http):
    http.router.get(f"{G}/a").respond(200, json={"id": "1"})
    http.router.get(f"{G}/b").respond(200, json=[1, 2])
    http.router.delete(f"{G}/c").respond(200, json=True)
    http.router.get(f"{G}/d").respond(200, content=b"")
    http.router.get(f"{G}/e").respond(200, content=b"<html>")
    assert mc.graph_request(http, "GET", f"{G}/a") == {"id": "1"}
    assert mc.graph_request(http, "GET", f"{G}/b") == {"data": [1, 2]}
    assert mc.graph_request(http, "DELETE", f"{G}/c") == {"success": True}
    assert mc.graph_request(http, "GET", f"{G}/d") == {}
    with pytest.raises(ProviderError) as exc:
        mc.graph_request(http, "GET", f"{G}/e")
    assert exc.value.kind == ErrorKind.TRANSIENT


def test_graph_request_transport_errors(http):
    http.router.get(f"{G}/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    http.router.get(f"{G}/down").mock(side_effect=httpx.ConnectError("down"))
    http.router.get(f"{G}/reset").mock(side_effect=httpx.RemoteProtocolError("reset"))
    with pytest.raises(ProviderError) as exc:
        mc.graph_request(http, "GET", f"{G}/slow")
    assert (exc.value.kind, exc.value.code) == (ErrorKind.TRANSIENT, "meta_timeout")
    with pytest.raises(ProviderError) as exc:
        mc.graph_request(http, "GET", f"{G}/down")
    assert exc.value.code == "meta_connect_failed"
    with pytest.raises(ProviderError) as exc:
        mc.graph_request(http, "GET", f"{G}/reset")
    assert exc.value.code == "meta_network"


def test_usage_headers_tracking_and_background_budget(http):
    http.router.get(f"{G}/me").respond(
        200,
        json={"id": "1"},
        headers={
            "x-app-usage": json.dumps({"call_count": 85, "total_cputime": 10, "total_time": 12}),
            "x-business-use-case-usage": json.dumps(
                {"9": [{"type": "instagram", "call_count": 40, "estimated_time_to_regain_access": 3}]}
            ),
        },
    )
    assert mc.usage_percent() == 0
    mc.ensure_background_budget()
    mc.graph_request(http, "GET", f"{G}/me")
    assert mc.usage_percent() == 85
    assert mc.background_paused()
    assert mc.usage_regain_seconds() == 180
    with pytest.raises(ProviderError) as exc:
        mc.ensure_background_budget()
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    assert exc.value.retry_after == 180
    mc.reset_usage()
    assert not mc.background_paused()


def test_parse_usage_headers_ignores_garbage():
    assert mc.parse_usage_headers({"x-app-usage": "not json"}) == []
    assert mc.parse_usage_headers(None) == []
    readings = mc.parse_usage_headers({"x-page-usage": json.dumps({"call_count": 3, "total_time": 9})})
    assert readings[0].percent == 9


def test_iter_paginated_follows_next_and_readds_auth(http):
    calls = []

    def handler(request):
        calls.append(request)
        if "after" in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "3"}]})
        return httpx.Response(
            200,
            json={
                "data": [{"id": "1"}, {"id": "2"}],
                "paging": {"next": f"{G}/me/accounts?limit=2&after=XYZ&access_token=OLD"},
            },
        )

    http.router.get(f"{G}/me/accounts").mock(side_effect=handler)
    items = list(mc.iter_paginated(http, f"{G}/me/accounts", {"access_token": "NEW", "limit": "2"}))
    assert [i["id"] for i in items] == ["1", "2", "3"]
    assert calls[1].url.params["access_token"] == "NEW"
    assert calls[1].url.params["after"] == "XYZ"


def test_iter_paginated_refuses_foreign_next_host(http):
    http.router.get(f"{G}/x").respond(
        200, json={"data": [{"id": "1"}], "paging": {"next": "https://evil.example/steal?access_token=tok"}}
    )
    assert [i["id"] for i in mc.iter_paginated(http, f"{G}/x", {"access_token": "t"})] == ["1"]


# ------------------------------------------------------------------------------------------- oauth


def test_build_login_url_scope_and_config_id():
    url = mc.build_login_url(settings(), "http://localhost:8765/conexiones/callback/meta", "STATE")
    parts = urlsplit(url)
    assert parts.netloc == "www.facebook.com" and parts.path == "/v25.0/dialog/oauth"
    query = parse_qs(parts.query)
    assert query["client_id"] == ["123456"]
    assert query["state"] == ["STATE"]
    assert query["response_type"] == ["code"]
    assert query["auth_type"] == ["rerequest"]
    assert query["scope"][0].split(",") == mc.SCOPES_FB_IG
    assert "app-secret" not in url
    url2 = mc.build_login_url(settings(), "http://localhost:8765/cb", "S", config_id="CFG")
    query2 = parse_qs(urlsplit(url2).query)
    assert query2["config_id"] == ["CFG"] and query2["override_default_response_type"] == ["true"]
    assert "scope" not in query2
    with pytest.raises(ProviderError) as exc:
        mc.build_login_url(settings(meta_app_id=None), "x", "s")
    assert exc.value.code == "meta_not_configured"


def test_exchange_code_short_then_long_lived(http):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if request.url.params.get("grant_type") == "fb_exchange_token":
            return httpx.Response(
                200, json={"access_token": "LONG", "token_type": "bearer", "expires_in": 5183944}
            )
        return httpx.Response(200, json={"access_token": "SHORT", "expires_in": 3600})

    http.router.get(f"{G}/oauth/access_token").mock(side_effect=handler)
    clock = ManualClock(NOW)
    token, expires = mc.exchange_code(settings(), http, "CODE", "http://localhost:8765/cb", clock=clock)
    assert token == "LONG"
    assert expires == NOW + timedelta(seconds=5183944)
    assert seen[0]["code"] == "CODE" and seen[0]["redirect_uri"] == "http://localhost:8765/cb"
    assert seen[0]["client_secret"] == "app-secret"
    assert seen[1]["fb_exchange_token"] == "SHORT"


def test_exchange_code_errors(http):
    http.router.get(f"{G}/oauth/access_token").respond(
        400, json=err(100, 36009, "This authorization code has been used.")
    )
    with pytest.raises(ProviderError) as exc:
        mc.exchange_code(settings(), http, "CODE", "http://localhost:8765/cb")
    assert exc.value.code == "meta_oauth_code_invalid"
    assert "CODE" not in json.dumps(exc.value.detail)
    with pytest.raises(ProviderError) as exc:
        mc.exchange_code(settings(meta_app_secret=None), http, "CODE", "x")
    assert exc.value.code == "meta_not_configured"


def test_list_pages_with_instagram_pagination_and_warnings(http):
    def handler(request):
        assert request.url.params["access_token"] == "USER"
        if request.url.params.get("after"):
            return httpx.Response(
                200,
                json={"data": [{"id": "222", "name": "Sin IG", "access_token": "PT2", "tasks": ["ANALYZE"]}]},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "111",
                        "name": "Mi Página",
                        "access_token": "PT1",
                        "tasks": ["CREATE_CONTENT", "MODERATE"],
                        "picture": {"data": {"url": "https://cdn/p.jpg"}},
                        "instagram_business_account": {
                            "id": "17841400000000001",
                            "username": "mimarca",
                            "name": "Mi Marca",
                            "profile_picture_url": "https://cdn/ig.jpg",
                            "followers_count": 1200,
                        },
                    },
                    {"id": "999", "name": "Sin token"},
                ],
                "paging": {"next": f"{G}/me/accounts?after=AAA&fields=id"},
            },
        )

    http.router.get(f"{G}/me/accounts").mock(side_effect=handler)
    pages = mc.list_pages(settings(), http, "USER")
    first_call = http.router.calls[0].request
    assert "instagram_business_account" in first_call.url.params["fields"]
    assert [p.id for p in pages] == ["111", "222"]
    first, second = pages
    assert first.access_token == "PT1" and first.picture_url == "https://cdn/p.jpg"
    assert first.instagram and first.instagram.id == "17841400000000001"
    assert first.instagram.username == "mimarca" and first.instagram.followers_count == 1200
    assert first.can_publish and mc.page_warnings(first) == []
    assert not second.can_publish and second.instagram is None
    warnings = mc.page_warnings(second)
    assert any("CREATE_CONTENT" in w for w in warnings)
    assert any("Instagram" in w for w in warnings)


def test_list_pages_business_walk_is_best_effort(http):
    http.router.get(f"{G}/me/accounts").respond(200, json={"data": []})
    http.router.get(f"{G}/me/businesses").respond(200, json={"data": [{"id": "B1"}]})
    http.router.get(f"{G}/B1/owned_pages").respond(
        200, json={"data": [{"id": "333", "name": "Owned", "access_token": "PT3"}]}
    )
    http.router.get(f"{G}/B1/client_pages").respond(400, json=err(100, None, "Unsupported"))
    pages = mc.list_pages(settings(), http, "USER", include_business=True)
    assert [p.id for p in pages] == ["333"]


def test_permissions_and_debug_token(http):
    http.router.get(f"{G}/me/permissions").respond(
        200,
        json={
            "data": [
                {"permission": "pages_show_list", "status": "granted"},
                {"permission": "read_insights", "status": "declined"},
            ]
        },
    )
    granted = mc.granted_permissions(settings(), http, "USER")
    assert granted == ["pages_show_list"]
    assert "read_insights" in mc.missing_scopes(granted)

    def debug(request):
        assert request.url.params["access_token"] == "123456|app-secret"
        return httpx.Response(200, json={"data": {"is_valid": True, "expires_at": 0}})

    http.router.get(f"{G}/debug_token").mock(side_effect=debug)
    assert mc.debug_token(settings(), http, "PT")["is_valid"] is True


# -------------------------------------------------------------------------------------------- rupload


def test_rupload_sends_bytes_with_oauth_header(http, tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 3000)
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = request.read()
        return httpx.Response(200, json={"success": True})

    url = "https://rupload.facebook.com/video-upload/v25.0/555"
    http.router.post(url).mock(side_effect=handler)
    assert mc.rupload(http, url, "PAGETOKEN", video) == {"success": True}
    assert seen["headers"]["authorization"] == "OAuth PAGETOKEN"
    assert seen["headers"]["offset"] == "0"
    assert seen["headers"]["file_size"] == "3000"
    assert seen["headers"]["content-length"] == "3000"
    assert seen["body"] == b"x" * 3000


@pytest.mark.parametrize(
    "url",
    [
        "http://rupload.facebook.com/video-upload/v25.0/1",
        "https://evil.example/video-upload/v25.0/1",
        "https://rupload.facebook.com:8443/video-upload/1",
        "https://rupload.facebook.com/other/1",
        "https://user@rupload.facebook.com/video-upload/1",
    ],
)
def test_rupload_refuses_unexpected_urls(http, tmp_path: Path, url):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ProviderError) as exc:
        mc.rupload(http, url, "TOKEN", video, prefixes=("/video-upload/",))
    assert exc.value.code == "meta_bad_upload_url"
    assert not http.router.calls


def test_rupload_failures(http, tmp_path: Path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    url = "https://rupload.facebook.com/ig-api-upload/v25.0/77"
    http.router.post(url).mock(
        side_effect=[
            httpx.Response(200, json={"success": False}),
            httpx.Response(400, json={"debug_info": {"retriable": False, "message": "corrupt"}}),
        ]
    )
    with pytest.raises(ProviderError) as exc:
        mc.rupload(http, url, "T", video)
    assert exc.value.kind == ErrorKind.TRANSIENT
    with pytest.raises(ProviderError) as exc:
        mc.rupload(http, url, "T", video)
    assert exc.value.kind == ErrorKind.PERMANENT
    with pytest.raises(ProviderError) as exc:
        mc.rupload(http, url, None, video)
    assert exc.value.kind == ErrorKind.AUTH
    with pytest.raises(ProviderError) as exc:
        mc.rupload(http, url, "T", tmp_path / "missing.mp4")
    assert exc.value.code == "media_missing"


# ------------------------------------------------------------------------------------------- insights


def test_fetch_insights_falls_back_metric_by_metric(http):
    def handler(request):
        metric = request.url.params["metric"]
        if "," in metric or metric == "page_impressions":
            return httpx.Response(
                400, json=err(100, None, "(#100) The value must be a valid insights metric")
            )
        return httpx.Response(200, json={"data": [{"name": metric, "values": [{"value": 7}]}]})

    http.router.get(f"{G}/1/insights").mock(side_effect=handler)
    out = mc.fetch_insights(
        http, f"{G}/1/insights", {"access_token": "t"}, ["page_media_view", "page_impressions"]
    )
    assert list(out) == ["page_media_view"]
    assert mc.insight_value(out["page_media_view"]) == 7


def test_fetch_insights_propagates_auth_and_rate_limits(http):
    http.router.get(f"{G}/1/insights").respond(400, json=err(190, 463))
    with pytest.raises(ProviderError) as exc:
        mc.fetch_insights(http, f"{G}/1/insights", {}, ["a", "b"])
    assert exc.value.kind == ErrorKind.AUTH
    http.router.get(f"{G}/2/insights").respond(400, json=err(4))
    with pytest.raises(ProviderError) as exc:
        mc.fetch_insights(http, f"{G}/2/insights", {}, ["a"])
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    http.router.get(f"{G}/3/insights").respond(400, json=err(100, None, "valid insights metric"))
    assert mc.fetch_insights(http, f"{G}/3/insights", {}, ["a"]) == {}
    assert mc.fetch_insights(http, f"{G}/3/insights", {}, []) == {}


def test_insight_value_shapes():
    day = date(2026, 11, 2)
    entry = {
        "values": [
            {"value": 5, "end_time": "2026-11-02T08:00:00+0000"},
            {"value": 9, "end_time": "2026-11-03T08:00:00+0000"},
        ]
    }
    assert mc.insight_value(entry, day) == 9
    assert mc.insight_value(entry) == 9
    assert mc.insight_value({"total_value": {"value": 42}}) == 42
    assert (
        mc.insight_value(
            {"total_value": {"breakdowns": [{"results": [{"value": 2}, {"value": 3}]}]}},
        )
        == 5
    )
    assert mc.insight_value({"values": [{"value": {"a": 1, "b": 2}}]}) == 3
    assert mc.insight_value({"values": []}) is None
    assert mc.engagement({"interactions": 5, "reach": 100}) == 5.0
    assert mc.engagement({"interactions": 5, "views": 50}) == 10.0
    assert mc.engagement({"interactions": 5}) is None
    assert mc.day_window(date(2026, 1, 1)) == (1767225600, 1767312000)


def test_insight_value_never_returns_another_days_number():
    """Re-reading a completed day must not store a neighbouring day's value under it (core review [12])."""
    day = date(2026, 11, 2)
    only_previous = {"values": [{"value": 5, "end_time": "2026-11-02T08:00:00+0000"}]}
    assert mc.insight_value(only_previous, day) is None
    assert mc.insight_value(only_previous) == 5
    utc_closed = {"values": [{"value": 7, "end_time": "2026-11-03T00:00:00+0000"}]}
    assert mc.insight_value(utc_closed, day) == 7
    undated = {"values": [{"value": 4}]}
    assert mc.insight_value(undated, day) == 4
    since, until = mc.series_window(day)
    assert until - since == 2 * 86400 and since == mc.day_window(day)[0]


# --------------------------------------------------------------------------------------------- images


def _image(tmp_path: Path, name: str, fmt: str, size=(800, 600), mode="RGB") -> MediaInfo:
    from PIL import Image

    path = tmp_path / name
    Image.new(mode, size, (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30)).save(path, fmt)
    mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[fmt]
    return MediaInfo(
        id=1,
        kind="image",
        path=path,
        mime=mime,
        size_bytes=path.stat().st_size,
        width=size[0],
        height=size[1],
        duration_s=None,
        fps=None,
        video_codec=None,
        audio_codec=None,
        bitrate=None,
        is_hdr=False,
        original_name=name,
    )


def test_prepare_image_passthrough_and_conversion(tmp_path: Path):
    jpeg = _image(tmp_path, "a.jpg", "JPEG")
    assert mc.prepare_image(jpeg, 8_000_000) == (jpeg.path, "image/jpeg")
    png = _image(tmp_path, "b.png", "PNG", mode="RGBA")
    assert mc.prepare_image(png, 4_000_000, accept=("image/jpeg", "image/png")) == (png.path, "image/png")
    converted, mime = mc.prepare_image(png, 8_000_000)
    assert mime == "image/jpeg" and converted.suffix == ".jpg" and converted != png.path
    from PIL import Image

    with Image.open(converted) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
    again, _ = mc.prepare_image(png, 8_000_000)
    assert again == converted  # cached
    webp = _image(tmp_path, "c.webp", "WEBP", size=(1600, 1600))
    small, _ = mc.prepare_image(webp, 30_000)
    assert small.stat().st_size <= 30_000


def test_prepare_image_errors(tmp_path: Path):
    bad = _image(tmp_path, "a.png", "PNG")
    bad.path.write_bytes(b"not an image")
    with pytest.raises(ProviderError) as exc:
        mc.prepare_image(bad, 8_000_000)
    assert exc.value.code == "image_conversion_failed"
    bad.path.unlink()
    with pytest.raises(ProviderError) as exc:
        mc.prepare_image(bad, 8_000_000)
    assert exc.value.code == "media_missing"


# --------------------------------------------------------------------------------------- run_commit


def _ctx(http: httpx.Client) -> tuple[PublishContext, list, list]:
    saved: list = []
    commits: list = []
    account = AccountCreds(1, "facebook", "111", "Página", "page", "PT", None, None, None, {})
    target = TargetSpec("facebook", "post", "hola", [], {}, None, True)
    ctx = PublishContext(
        account=account,
        target=target,
        target_id=1,
        state={"stage": "commit"},
        settings=settings(),
        http=http,
        clock=ManualClock(NOW),
        public_media=None,
        save_state=lambda s: saved.append(dict(s)),
        before_commit=lambda s: commits.append(dict(s)),
        log=lambda *a: None,
    )
    return ctx, saved, commits


def test_run_commit_marks_state_and_converts_errors(http):
    ctx, saved, commits = _ctx(http)
    assert mc.run_commit(ctx, "Facebook", lambda: {"id": "1"}, sent={"message": "hola"}) == {"id": "1"}
    assert commits[0]["stage"] == "committing" and commits[0]["commit_stage"] == "commit"
    assert commits[0]["commit_at"] == NOW.isoformat() and commits[0]["sent"] == {"message": "hola"}

    def timeout():
        raise ProviderError(ErrorKind.TRANSIENT, "meta_timeout", "x")

    ctx, saved, commits = _ctx(http)
    with pytest.raises(ProviderError) as exc:
        mc.run_commit(ctx, "Facebook", timeout)
    assert exc.value.kind == ErrorKind.AMBIGUOUS
    assert ctx.state["stage"] == "committing" and "commit_rejected" not in ctx.state

    def refused():
        raise ProviderError(ErrorKind.PERMANENT, "fb_duplicate", "x")

    ctx, saved, commits = _ctx(http)
    with pytest.raises(ProviderError) as exc:
        mc.run_commit(ctx, "Facebook", refused)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert ctx.state["commit_rejected"] is True and ctx.state["stage"] == "commit"
    assert "commit_at" not in ctx.state and saved

    def not_connected():
        raise ProviderError(ErrorKind.TRANSIENT, "meta_connect_failed", "x")

    ctx, saved, commits = _ctx(http)
    with pytest.raises(ProviderError) as exc:
        mc.run_commit(ctx, "Facebook", not_connected)
    assert exc.value.kind == ErrorKind.TRANSIENT and ctx.state["commit_rejected"] is True


def test_run_commit_uncertain_refusal_is_ambiguous_keeping_the_original(http):
    original = mc.classify_meta_error(400, err(4, 2207051, "We restrict certain activity"), {})

    def blocked():
        raise original

    ctx, saved, commits = _ctx(http)
    with pytest.raises(ProviderError) as exc:
        mc.run_commit(ctx, "Instagram", blocked)
    assert exc.value.kind == ErrorKind.AMBIGUOUS and exc.value.code == "ig_activity_restricted"
    assert exc.value.__cause__ is original and exc.value.retry_after == mc.ACTION_BLOCK_WAIT
    assert exc.value.detail["refused_kind"] == "permanent"
    assert ctx.state["stage"] == "committing" and "commit_rejected" not in ctx.state


def test_prepare_image_prefers_media_derived_jpeg_via_current_database(tmp_path: Path, monkeypatch):
    from planificador import db as dbmod
    from planificador import media
    from planificador.models import MediaAsset

    database = dbmod.Database(tmp_path / "t.sqlite3")
    database.init()
    dbmod.set_current_database(database)  # reset by conftest's autouse fixture
    with database.session_scope() as session:
        asset = MediaAsset(
            kind="image",
            original_name="a.png",
            rel_path="ab/a.png",
            sha256="a" * 64,
            size_bytes=10,
            mime="image/png",
        )
        session.add(asset)
        session.flush()
        asset_id = asset.id
    derived = tmp_path / "derived.jpg"
    derived.write_bytes(b"\xff\xd8jpeg")
    seen: dict = {}

    def fake_derived(found, max_bytes=8_000_000):
        seen.update(id=found.id, max=max_bytes)
        return derived

    monkeypatch.setattr(media, "derived_jpeg", fake_derived)
    info = _image(tmp_path, "a.png", "PNG")
    info.id = asset_id
    assert mc.prepare_image(info, 8_000_000) == (derived, "image/jpeg")
    assert seen == {"id": asset_id, "max": 8_000_000}
    jpeg = _image(tmp_path, "b.jpg", "JPEG")
    jpeg.id = asset_id
    assert mc.prepare_image(jpeg, 8_000_000, prefer_derived=True) == (derived, "image/jpeg")

    def broken(found, max_bytes=8_000_000):
        raise OSError("disk full")

    monkeypatch.setattr(media, "derived_jpeg", broken)
    path, mime = mc.prepare_image(info, 8_000_000)  # falls back to the local conversion
    assert mime == "image/jpeg" and path != derived and path.suffix == ".jpg"
    database.dispose()

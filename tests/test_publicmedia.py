"""Public media (SPEC §9): token-only public server, modes off/tunnel/base_url, cloudflared manager.

The tunnel tests run a fake ``cloudflared`` (a small Python script with the same flags that serves the
``/quicktunnel`` metrics endpoint), so nothing leaves the machine.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import textwrap
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from planificador import publicmedia
from planificador.models import PublicMediaToken
from planificador.providers.base import ErrorKind, ProviderError
from planificador.publicmedia import (
    CloudflaredTunnel,
    PublicMediaApp,
    PublicMediaError,
    PublicMediaService,
    platform_asset,
)

FAKE_HOST = "fake-tunnel-1234.trycloudflare.com"

FAKE_CLOUDFLARED = textwrap.dedent(
    """
    import json, os, signal, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    args = sys.argv[1:]
    mode = os.environ.get("FAKE_CF_MODE", "ok")
    host = os.environ.get("FAKE_CF_HOST", "fake-tunnel-1234.trycloudflare.com")
    with open(os.environ["FAKE_CF_ARGS"], "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    if mode == "die":
        print("failed to request quick Tunnel", file=sys.stderr, flush=True)
        sys.exit(3)
    mhost, mport = args[args.index("--metrics") + 1].split(":")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/quicktunnel" and mode == "ok":
                body = json.dumps({"hostname": host}).encode()
            elif self.path == "/quicktunnel":
                body = json.dumps({"hostname": ""}).encode()
            elif self.path == "/ready":
                body = b"{}"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer((mhost, int(mport)), Handler)
    print("INF Requesting new quick Tunnel on trycloudflare.com...", file=sys.stderr, flush=True)
    print("INF |  https://" + host + "  |", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    server.serve_forever()
    """
)


# ------------------------------------------------------------------------------------------- fixtures


class Settings(dict):
    def __call__(self) -> dict:
        return dict(self)


@pytest.fixture
def settings() -> Settings:
    return Settings(public_media_mode="off", public_media_base_url=None)


@pytest.fixture
def media_file(app: Any) -> Path:
    path = Path(app.state.paths.derived_dir) / "foto-publica.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg-bytes" * 1000)
    return path


@pytest.fixture
def fake_cf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    script = tmp_path / "fake_cloudflared.py"
    script.write_text(FAKE_CLOUDFLARED)
    args_file = tmp_path / "cf-args.jsonl"
    monkeypatch.setenv("FAKE_CF_ARGS", str(args_file))
    monkeypatch.setenv("FAKE_CF_MODE", "ok")
    return {"command": [sys.executable, str(script)], "args": args_file}


@pytest.fixture
def services() -> Iterator[list[PublicMediaService]]:
    created: list[PublicMediaService] = []
    yield created
    for service in created:
        service.stop()


def make_service(
    app: Any,
    settings: Settings,
    services: list[PublicMediaService],
    *,
    command: list[str] | None = None,
    **kwargs: Any,
) -> PublicMediaService:
    tunnel = CloudflaredTunnel(Path(app.state.paths.bin_dir), command=command, hostname_timeout=10)
    kwargs.setdefault("verify_public", False)
    service = PublicMediaService(
        app.state.db.session,
        app.state.settings.data_dir,
        settings,
        port=0,
        clock=app.state.clock,
        tunnel=tunnel,
        **kwargs,
    )
    services.append(service)
    return service


def add_token(db: Session, rel_path: str, *, mime: str = "image/jpeg", minutes: int = 60, now=None) -> str:
    from planificador.models import utcnow

    now = now or utcnow()
    token = "T" + hashlib.sha256(f"{rel_path}{minutes}{time.time()}".encode()).hexdigest()[:21]
    db.add(
        PublicMediaToken(
            token=token, rel_path=rel_path, mime=mime, expires_at=now + timedelta(minutes=minutes)
        )
    )
    db.commit()
    return token


# ----------------------------------------------------------------------------------------- public app


@pytest.fixture
def public_client(app: Any) -> Iterator[TestClient]:
    papp = PublicMediaApp(app.state.db.session, app.state.settings.data_dir, clock=app.state.clock.now)
    with TestClient(papp, base_url="http://127.0.0.1:8766") as client:
        yield client


def test_public_app_serves_only_valid_tokens(
    app: Any, db: Session, media_file: Path, public_client, clock
) -> None:
    rel = media_file.relative_to(app.state.settings.data_dir).as_posix()
    token = add_token(db, rel, now=clock.now())
    r = public_client.get(f"/m/{token}")
    assert r.status_code == 200 and r.content == media_file.read_bytes()
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-length"] == str(media_file.stat().st_size)
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in r.headers
    assert public_client.get(f"/m/{token}.jpg").status_code == 200
    head = public_client.head(f"/m/{token}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(media_file.stat().st_size)
    db.expire_all()
    assert db.get(PublicMediaToken, token).hits == 3

    clock.advance(minutes=61)
    assert public_client.get(f"/m/{token}").status_code == 404  # expired


def test_public_app_404s(app: Any, db: Session, media_file: Path, public_client, clock) -> None:
    rel = media_file.relative_to(app.state.settings.data_dir).as_posix()
    revoked = add_token(db, rel, now=clock.now())
    row = db.get(PublicMediaToken, revoked)
    row.revoked_at = clock.now()
    db.commit()
    outside = add_token(db, "../outside.jpg", now=clock.now())
    missing = add_token(db, "media/derived/no-existe.jpg", now=clock.now())
    good = add_token(db, rel, now=clock.now())
    for path in (
        f"/m/{revoked}",
        f"/m/{outside}",
        f"/m/{missing}",
        "/m/desconocido-0123456789abcdef",
        "/m/short",
        "/m/../planificador.sqlite3",
        f"/m/{good}/extra",
        "/",
        "/biblioteca",
        "/static/css/app.css",
        "/healthz",
    ):
        r = public_client.get(path)
        assert r.status_code == 404, path
        assert r.headers[publicmedia.MARKER_HEADER] == "1"
    assert public_client.post(f"/m/{good}").status_code == 404
    assert public_client.delete(f"/m/{good}").status_code == 404


def test_public_app_rate_limits_a_token(app: Any, db: Session, media_file: Path, clock) -> None:
    papp = PublicMediaApp(app.state.db.session, app.state.settings.data_dir, clock=clock.now, rate_limit=3)
    rel = media_file.relative_to(app.state.settings.data_dir).as_posix()
    token = add_token(db, rel, now=clock.now())
    with TestClient(papp) as client:
        codes = [client.head(f"/m/{token}").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# ---------------------------------------------------------------------------------------------- modes


def test_mode_off_raises_permanent_error(app: Any, settings: Settings, services, media_file: Path) -> None:
    service = make_service(app, settings, services)
    with pytest.raises(ProviderError) as exc:
        service.url_for(media_file, "image/jpeg")
    assert exc.value.kind == ErrorKind.PERMANENT and exc.value.code == "public_media_off"
    assert exc.value.message == "Para publicar fotos en Instagram activa la URL pública en Ajustes."
    service.start()
    assert not service.server.running  # nothing starts while off
    status = service.status()
    assert status["mode"] == "off" and status["enabled"] is False and status["tunnel"]["state"] == "stopped"


def test_base_url_mode_serves_through_real_server(
    app: Any, db: Session, settings: Settings, services, media_file: Path
) -> None:
    settings.update(public_media_mode="base_url", public_media_base_url="https://media.ejemplo.com/")
    service = make_service(app, settings, services)
    url = service.url_for(media_file, "image/jpeg", ttl_seconds=600)
    assert url.startswith("https://media.ejemplo.com/m/") and url.endswith(".jpg")
    token = service.token_from_url(url)
    assert token and len(token) >= 22
    row = db.get(PublicMediaToken, token)
    assert row.rel_path == "media/derived/foto-publica.jpg" and row.mime == "image/jpeg"
    assert row.expires_at - row.created_at == timedelta(seconds=600)
    assert service.server.running
    port = service.server.port
    r = httpx.get(f"http://127.0.0.1:{port}/m/{token}.jpg")
    assert r.status_code == 200 and r.content == media_file.read_bytes()
    assert httpx.get(f"http://127.0.0.1:{port}/").status_code == 404
    assert service.status()["active_tokens"] == 1

    service.release(url)
    service.release(url)  # idempotent
    service.release("https://otro.example.com/no-es-un-token")
    db.expire_all()
    assert db.get(PublicMediaToken, token).revoked_at is not None
    assert httpx.get(f"http://127.0.0.1:{port}/m/{token}").status_code == 404
    service.stop()
    assert not service.server.running


@pytest.mark.parametrize(
    "value", [None, "", "http://inseguro.example.com", "https://x.example.com/?a=1", "nota"]
)
def test_base_url_must_be_https(app: Any, settings: Settings, services, media_file: Path, value: Any) -> None:
    settings.update(public_media_mode="base_url", public_media_base_url=value)
    service = make_service(app, settings, services)
    with pytest.raises(ProviderError) as exc:
        service.url_for(media_file, "image/jpeg")
    assert exc.value.code == "public_media_base_url_missing" and exc.value.kind == ErrorKind.PERMANENT


def test_only_files_inside_the_data_dir(app: Any, settings: Settings, services, tmp_path: Path) -> None:
    settings.update(public_media_mode="base_url", public_media_base_url="https://m.example.com")
    service = make_service(app, settings, services)
    outside = tmp_path / "fuera.jpg"
    outside.write_bytes(b"x")
    with pytest.raises(ProviderError) as exc:
        service.url_for(outside, "image/jpeg")
    assert exc.value.code == "public_media_path"
    with pytest.raises(ProviderError) as exc:
        service.url_for(Path(app.state.paths.derived_dir) / "nada.jpg", "image/jpeg")
    assert exc.value.code == "media_missing"


def test_ttl_is_clamped(app: Any, db: Session, settings: Settings, services, media_file: Path) -> None:
    settings.update(public_media_mode="base_url", public_media_base_url="https://m.example.com")
    service = make_service(app, settings, services)
    short = db.get(PublicMediaToken, service.token_from_url(service.url_for(media_file, "image/jpeg", 1)))
    assert short.expires_at - short.created_at == timedelta(seconds=60)
    long = db.get(PublicMediaToken, service.token_from_url(service.url_for(media_file, "video/mp4", 10**9)))
    assert long.expires_at - long.created_at == timedelta(hours=24)


# --------------------------------------------------------------------------------------------- tunnel


def test_tunnel_mode_with_fake_cloudflared(
    app: Any, db: Session, settings: Settings, services, media_file: Path, fake_cf: dict
) -> None:
    settings.update(public_media_mode="tunnel")
    service = make_service(app, settings, services, command=fake_cf["command"])
    url = service.url_for(media_file, "image/jpeg")
    assert url.startswith(f"https://{FAKE_HOST}/m/")
    args = json.loads(fake_cf["args"].read_text().splitlines()[-1])
    assert args[0] == "tunnel" and "--no-autoupdate" in args
    assert args[args.index("--url") + 1] == f"http://127.0.0.1:{service.server.port}"
    assert args[args.index("--metrics") + 1].startswith("127.0.0.1:")
    config_file = Path(args[args.index("--config") + 1])
    assert config_file.parent == Path(app.state.paths.bin_dir) and config_file.is_file()
    status = service.status()
    assert status["tunnel"]["state"] == "running" and status["tunnel"]["hostname"] == FAKE_HOST
    assert status["public_base"] == f"https://{FAKE_HOST}" and status["tunnel"]["pid"]
    assert (Path(app.state.paths.bin_dir) / "cloudflared.pid").is_file()

    # A second URL reuses the running tunnel.
    service.url_for(media_file, "image/jpeg")
    assert len(fake_cf["args"].read_text().splitlines()) == 1

    # Busy (active tokens) → kept; idle for 5 min without tokens → stopped.
    assert service.check_idle(time.monotonic() + 3600) == "ok"
    service.revoke_all()
    assert service.check_idle(time.monotonic() + 10) == "ok"  # not idle long enough
    assert service.check_idle(time.monotonic() + publicmedia.IDLE_SHUTDOWN_S + 1) == "idle_stop"
    assert not service.tunnel.alive and service.status()["tunnel"]["state"] == "stopped"

    # Next request starts it again.
    assert service.url_for(media_file, "image/jpeg").startswith(f"https://{FAKE_HOST}/")
    assert len(fake_cf["args"].read_text().splitlines()) == 2
    proc = service.tunnel.proc
    service.stop()
    assert proc is not None and proc.poll() is not None


def test_tunnel_restarts_when_it_dies_with_active_tokens(
    app: Any, settings: Settings, services, media_file: Path, fake_cf: dict
) -> None:
    settings.update(public_media_mode="tunnel")
    service = make_service(app, settings, services, command=fake_cf["command"])
    service.url_for(media_file, "image/jpeg")
    service.tunnel.proc.kill()
    service.tunnel.proc.wait(5)
    assert service.status()["tunnel"]["state"] == "error"
    assert service.check_idle() == "restart"
    assert service.tunnel.alive and service.tunnel.restarts == 1


def test_hostname_fallback_from_logs(
    app: Any, settings: Settings, services, media_file: Path, fake_cf, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CF_MODE", "logs")
    settings.update(public_media_mode="tunnel")
    service = make_service(app, settings, services, command=fake_cf["command"])
    assert service.url_for(media_file, "image/jpeg").startswith(f"https://{FAKE_HOST}/m/")


def test_tunnel_failure_is_transient(
    app: Any, settings: Settings, services, media_file: Path, fake_cf, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CF_MODE", "die")
    settings.update(public_media_mode="tunnel")
    service = make_service(app, settings, services, command=fake_cf["command"])
    with pytest.raises(ProviderError) as exc:
        service.url_for(media_file, "image/jpeg")
    assert exc.value.kind == ErrorKind.TRANSIENT and exc.value.code == "tunnel_unavailable"
    tunnel = service.status()["tunnel"]
    assert tunnel["state"] == "error" and "quick Tunnel" in tunnel["error"]


def test_tunnel_self_test_through_the_public_url(
    app: Any, settings: Settings, services, media_file: Path, fake_cf: dict
) -> None:
    settings.update(public_media_mode="tunnel")
    service = make_service(app, settings, services, command=fake_cf["command"], verify_public=True)
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="127.0.0.1").pass_through()
        probe = mock.get(host=FAKE_HOST).mock(
            side_effect=[
                httpx.Response(530),  # Cloudflare: tunnel not reachable yet
                httpx.Response(404, headers={publicmedia.MARKER_HEADER: "1"}),
            ]
        )
        url = service.url_for(media_file, "image/jpeg")
        assert url.startswith(f"https://{FAKE_HOST}/")
        assert probe.call_count == 2
        service.url_for(media_file, "image/jpeg")  # verified once per hostname
        assert probe.call_count == 2


def test_probe_public_gives_up(app: Any, settings: Settings, services) -> None:
    service = make_service(app, settings, services)
    with respx.mock:
        respx.get(host="m.example.com").mock(return_value=httpx.Response(502))
        ok, message = service.probe_public("https://m.example.com", timeout=0.2)
    assert not ok and "HTTP 502" in message


# ------------------------------------------------------------------------------------ binary download


def _tunnel(tmp_path: Path, asset: str, payload: bytes, version: str = "9.9.9") -> CloudflaredTunnel:
    return CloudflaredTunnel(
        tmp_path / "bin",
        version=version,
        asset=asset,
        checksums={version: {asset: hashlib.sha256(payload).hexdigest()}},
    )


def test_binary_download_is_verified_and_cached(tmp_path: Path) -> None:
    payload = b"\x7fELF fake cloudflared"
    tunnel = _tunnel(tmp_path, "cloudflared-linux-amd64", payload)
    assert not tunnel.binary_ready()
    url = "https://github.com/cloudflare/cloudflared/releases/download/9.9.9/cloudflared-linux-amd64"
    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(200, content=payload))
        path = tunnel.ensure_binary()
        assert path == tmp_path / "bin" / "cloudflared-9.9.9"
        assert path.read_bytes() == payload and os.access(path, os.X_OK)
        assert tunnel.binary_ready()
        tunnel.ensure_binary()
        assert route.call_count == 1  # cached
    # A tampered binary is detected and downloaded again.
    path.write_bytes(b"tampered")
    fresh = _tunnel(tmp_path, "cloudflared-linux-amd64", payload)
    assert not fresh.binary_ready()


def test_binary_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path, "cloudflared-windows-amd64.exe", b"expected")
    url = "https://github.com/cloudflare/cloudflared/releases/download/9.9.9/cloudflared-windows-amd64.exe"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"malicious"))
        with pytest.raises(PublicMediaError) as exc:
            tunnel.ensure_binary()
    assert exc.value.code == "cloudflared_checksum" and not exc.value.transient
    assert list((tmp_path / "bin").iterdir()) == []


def test_binary_download_http_error_is_transient(tmp_path: Path) -> None:
    tunnel = _tunnel(tmp_path, "cloudflared-linux-arm64", b"x")
    with respx.mock:
        respx.get(url__regex=r".*/9\.9\.9/cloudflared-linux-arm64$").mock(return_value=httpx.Response(404))
        with pytest.raises(PublicMediaError) as exc:
            tunnel.ensure_binary()
    assert exc.value.code == "cloudflared_download" and exc.value.transient


def test_darwin_archive_is_extracted(tmp_path: Path) -> None:
    binary = b"\xcf\xfa\xed\xfe mach-o"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("cloudflared")
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    archive = buffer.getvalue()
    tunnel = _tunnel(tmp_path, "cloudflared-darwin-arm64.tgz", archive)
    with respx.mock:
        respx.get(url__regex=r".*/cloudflared-darwin-arm64\.tgz$").mock(
            return_value=httpx.Response(200, content=archive)
        )
        assert tunnel.ensure_binary().read_bytes() == binary


def test_pinned_table_covers_every_platform() -> None:
    assets = {
        "cloudflared-windows-amd64.exe",
        "cloudflared-windows-386.exe",
        "cloudflared-linux-amd64",
        "cloudflared-linux-arm64",
        "cloudflared-darwin-amd64.tgz",
        "cloudflared-darwin-arm64.tgz",
    }
    table = publicmedia.CLOUDFLARED_SHA256[publicmedia.CLOUDFLARED_VERSION]
    assert set(table) == assets
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in table.values())


@pytest.mark.parametrize(
    ("system", "machine", "asset"),
    [
        ("win32", "AMD64", "cloudflared-windows-amd64.exe"),
        ("win32", "ARM64", "cloudflared-windows-amd64.exe"),
        ("win32", "x86", "cloudflared-windows-386.exe"),
        ("linux", "x86_64", "cloudflared-linux-amd64"),
        ("linux", "aarch64", "cloudflared-linux-arm64"),
        ("darwin", "arm64", "cloudflared-darwin-arm64.tgz"),
        ("darwin", "x86_64", "cloudflared-darwin-amd64.tgz"),
    ],
)
def test_platform_asset(system: str, machine: str, asset: str) -> None:
    assert platform_asset(system, machine) == asset


def test_platform_asset_unsupported() -> None:
    with pytest.raises(PublicMediaError) as exc:
        platform_asset("freebsd", "riscv64")
    assert not exc.value.transient


def test_stale_pid_file_of_another_process_is_not_killed(tmp_path: Path) -> None:
    tunnel = CloudflaredTunnel(tmp_path / "bin", command=["x"])
    tunnel.bin_dir.mkdir(parents=True)
    tunnel.pid_file.write_text(str(os.getpid()))
    tunnel.kill_stale()  # we are not cloudflared: must survive
    assert not tunnel.pid_file.exists()


# ------------------------------------------------------------------------------------------ app wiring


def test_get_service_is_lazy_and_cached(app: Any, db: Session) -> None:
    from planificador import config

    assert app.state.public_media is None
    service = publicmedia.get_service(app)
    assert publicmedia.get_service(app.state) is service and app.state.public_media is service
    assert service.stop in app.state.shutdown_callbacks
    assert service.port == app.state.settings.pubmedia_port
    assert not service.server.running and service.mode() == "off"
    config.set_setting(db, "public_media_mode", "base_url")
    config.set_setting(db, "public_media_base_url", "https://media.ejemplo.com")
    status = service.status()
    assert status["mode"] == "base_url" and status["public_base"] == "https://media.ejemplo.com"
    assert status["mode_label"] == "URL pública propia"


def test_scheduler_getter_finds_the_service(app: Any) -> None:
    from planificador import scheduler

    getter = scheduler._public_media_getter(app.state)
    assert isinstance(getter(), PublicMediaService)

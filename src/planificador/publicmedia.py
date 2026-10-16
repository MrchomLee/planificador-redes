"""Public media: a tiny token-only file server + Cloudflare quick tunnel manager (SPEC §9).

Instagram fetches photos from a public HTTPS URL (``image_url``); a local app has none. This module provides
one, three ways (setting ``public_media_mode``):

- ``off`` — :meth:`PublicMediaService.url_for` raises ``ProviderError(PERMANENT, "public_media_off")``.
- ``tunnel`` — a Cloudflare *quick tunnel* (``https://<random>.trycloudflare.com``, no account) started on
  demand with the ``cloudflared`` binary, which is downloaded **only after the user enables this mode**
  (pinned release, SHA-256 verified against :data:`CLOUDFLARED_SHA256`) into ``<data>/bin``. The hostname is
  read from the metrics endpoint ``/quicktunnel`` (fallback: the log line). The tunnel stops after
  :data:`IDLE_SHUTDOWN_S` without active tokens and restarts when it dies.
- ``base_url`` — the user's own public HTTPS base URL (named tunnel, reverse proxy...) forwarding to our
  public port.

The public server is a separate minimal ASGI app (:class:`PublicMediaApp`) bound to ``127.0.0.1:<port+1>``,
run by Uvicorn in a background thread. It answers **only** ``GET``/``HEAD /m/<token>[.ext]`` for unexpired,
unrevoked rows of ``public_media_tokens`` (128-bit ``secrets.token_urlsafe(16)``), with the right
``Content-Type``/``Content-Length``; everything else is 404 (no listing, no redirects, no cookies). The UI is
never reachable through it. Research: ``docs/research/04-arquitectura-local.md`` §4.3–§4.6 (verificación #5,
#6, #10) and ``02-meta-api.md`` §4.3.

Contract: ``PublicMediaService(db_factory, data_dir, settings_getter)`` with ``url_for`` / ``release`` /
``start`` / ``stop`` / ``status()``; :func:`get_service` returns the app's (lazily created) service.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import logging
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from planificador.models import PublicMediaToken
from planificador.providers.base import ErrorKind, ProviderError

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------- constants

MODES = ("off", "tunnel", "base_url")
DEFAULT_PORT = 8766
DEFAULT_TTL_S = 3600  # 04 §4.4: 60 min (short TTLs make Meta's fetch fail)
MAX_TTL_S = 24 * 3600
IDLE_SHUTDOWN_S = 5 * 60  # SPEC §9: stop the tunnel after 5 idle minutes
WATCHDOG_INTERVAL_S = 15.0
HOSTNAME_TIMEOUT_S = 30.0
SELF_TEST_TIMEOUT_S = 60.0
RATE_LIMIT_PER_MIN = 60  # requests per token per minute (04 §4.6 dampens scanning)
RATE_WINDOW_S = 60.0
RATE_LIMIT_MAX_KEYS = 4096  # tokens tracked at once (made-up tokens must not grow memory)
SERVE_CHUNK = 256 * 1024
MARKER_HEADER = "x-planificador-public"  # lets the self-test recognise our own 404 through the tunnel

TOKEN_RE = re.compile(r"^/m/([A-Za-z0-9_-]{16,64})(?:\.[a-z0-9]{2,5})?$")
TUNNEL_HOST_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)")

MSG_OFF = "Para publicar fotos en Instagram activa la URL pública en Ajustes."
MSG_BASE_URL = (
    "Configura la URL pública base (https://…) en Ajustes, o elige el túnel temporal de Cloudflare."
)
MSG_TUNNEL = (
    "No se pudo exponer la imagen para Instagram: el túnel temporal de Cloudflare no ha arrancado. "
    "Se reintentará; si sigue fallando, revisa el cortafuegos/antivirus o usa una URL pública propia."
)

# Pinned cloudflared releases (Apache-2.0) and the SHA-256 of each asset as served by GitHub releases
# (https://github.com/cloudflare/cloudflared/releases/download/<version>/<asset>). Values computed on
# 2026-10-15 from the downloaded assets; the release notes carry the same "SHA256 Checksums" block
# (04 §4.3). Never download "latest": bump the version and the hashes together.
CLOUDFLARED_VERSION = "2026.10.1"
CLOUDFLARED_SHA256: dict[str, dict[str, str]] = {
    "2026.10.1": {
        "cloudflared-windows-amd64.exe": "4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6",
        "cloudflared-windows-386.exe": "a0c9a47af2cbd681005e4c1210edf95f986ecc66cee6d68b0e42c484af465d2e",
        "cloudflared-linux-amd64": "3bfc539f45d2620c71debb7a86be0efa737ed5ccbad9eb85c787116900e5c586",
        "cloudflared-linux-arm64": "2c65dfabf616c40efc7545d107d7a57ad5e21377e2964b7e88af19a238074a46",
        "cloudflared-darwin-amd64.tgz": "3b0dbaf378337e9d7fb6cd235a76d1d6a7dee434c2d8edf9281164fb6a3837ab",
        "cloudflared-darwin-arm64.tgz": "647615df99d49cd179ea5e13ef760b9bbfb33630f6ae62f87e33dfadda60adcc",
    },
    "2026.10.0": {
        "cloudflared-windows-amd64.exe": "86aee4017b26625cee8484c113558f48effa4cd47f7aa05fcf425604e5d2b23c",
        "cloudflared-windows-386.exe": "0630a8779e9823a1a3b091698b8e71874e0f7b205559219f52fdd301466b5546",
        "cloudflared-linux-amd64": "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db",
        "cloudflared-linux-arm64": "e6422b9d4f72d3194bc5a38676f13667c06666523217b842a877d72a80b5ac08",
        "cloudflared-darwin-amd64.tgz": "903845b81828c8cb3c5d13d816a2de71c06a3da5785469df8eb0e1b736d92f9f",
        "cloudflared-darwin-arm64.tgz": "a2f79ff7b9420aa537d74af239f376da170bbabeb529aec416002adac6a72e70",
    },
}
RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/download/{version}/{asset}"

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


class PublicMediaError(Exception):
    """Configuration/runtime problem of the public media service (Spanish ``message``)."""

    def __init__(self, code: str, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient

    def to_provider_error(self) -> ProviderError:
        kind = ErrorKind.TRANSIENT if self.transient else ErrorKind.PERMANENT
        return ProviderError(kind, self.code, self.message)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ================================================================================== public ASGI app


class _RateLimiter:
    """``per_minute`` requests per key in a fixed one-minute window, with bounded state.

    The keys are whatever a client puts in the URL, so the state must not grow with them: windows live in
    an insertion-ordered dict whose order is their start time, expired ones are dropped from its front on
    every call (amortised O(1), no scan), and beyond ``max_keys`` the oldest window is evicted (that key
    simply gets a fresh budget)."""

    def __init__(
        self,
        per_minute: int,
        *,
        window_s: float = RATE_WINDOW_S,
        max_keys: int = RATE_LIMIT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = per_minute
        self.window_s = window_s
        self.max_keys = max(1, max_keys)
        self._clock = clock
        self._windows: OrderedDict[str, list[float]] = OrderedDict()  # key → [window start, hits]
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._windows)

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            windows = self._windows
            while windows:
                start = next(iter(windows.values()))[0]
                if now - start < self.window_s:
                    break
                windows.popitem(last=False)
            entry = windows.get(key)
            if entry is None:
                windows[key] = [now, 1]
                if len(windows) > self.max_keys:
                    windows.popitem(last=False)
                return True
            if entry[1] >= self.per_minute:
                return False
            entry[1] += 1
            return True


@dataclass
class _Resolved:
    path: Path
    mime: str
    size: int


class PublicMediaApp:
    """Minimal ASGI app: ``GET|HEAD /m/<token>[.ext]`` → file, anything else → 404."""

    def __init__(
        self,
        db_factory: Callable[[], Session],
        data_dir: Path,
        clock: Callable[[], datetime] = _utcnow,
        *,
        rate_limit: int = RATE_LIMIT_PER_MIN,
        on_hit: Callable[[], None] | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.data_dir = Path(data_dir).resolve()
        self.clock = clock
        self.limiter = _RateLimiter(rate_limit)
        self.on_hit = on_hit

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":  # Uvicorn runs us with lifespan off; test clients do not
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        match = TOKEN_RE.match(path)
        status = 404
        try:
            if method in ("GET", "HEAD") and match:
                token = match.group(1)
                if not self.limiter.allow(token):
                    status = 429
                    await self._plain(send, 429, "Too Many Requests")
                    return
                resolved = await asyncio.to_thread(self._resolve, token)
                if resolved is not None:
                    status = 200
                    await self._file(send, resolved, head=method == "HEAD")
                    if self.on_hit:
                        self.on_hit()
                    return
            await self._plain(send, 404, "Not Found")
        finally:
            headers = dict(scope.get("headers") or [])
            ua = headers.get(b"user-agent", b"").decode("latin-1")[:120]
            prefix = match.group(1)[:6] if match else "-"
            log.info("Public media %s %s token=%s… → %s (UA %s)", method, path[:3], prefix, status, ua or "-")

    def _resolve(self, token: str) -> _Resolved | None:
        now = self.clock()
        with self.db_factory() as session:
            row = session.get(PublicMediaToken, token)
            if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
                return None
            target = (self.data_dir / row.rel_path).resolve()
            if self.data_dir not in target.parents or not target.is_file():
                return None
            session.execute(
                update(PublicMediaToken)
                .where(PublicMediaToken.token == token)
                .values(hits=PublicMediaToken.hits + 1)
            )
            session.commit()
            return _Resolved(target, row.mime, target.stat().st_size)

    @staticmethod
    def _headers(content_type: str, length: int) -> list[tuple[bytes, bytes]]:
        return [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(length).encode()),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (MARKER_HEADER.encode(), b"1"),
        ]

    async def _plain(self, send: Callable, status: int, text: str) -> None:
        body = text.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": self._headers("text/plain", len(body)),
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _file(self, send: Callable, resolved: _Resolved, *, head: bool) -> None:
        headers = self._headers(resolved.mime, resolved.size)
        headers.append((b"content-disposition", b"inline"))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        if head:
            await send({"type": "http.response.body", "body": b""})
            return
        fh = await asyncio.to_thread(open, resolved.path, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(fh.read, SERVE_CHUNK)
                more = len(chunk) == SERVE_CHUNK
                await send({"type": "http.response.body", "body": chunk, "more_body": more})
                if not more:
                    break
        finally:
            await asyncio.to_thread(fh.close)


# ===================================================================================== server thread


class PublicServer:
    """Runs :class:`PublicMediaApp` with Uvicorn in a daemon thread on ``127.0.0.1:<port>``."""

    def __init__(self, app: PublicMediaApp, port: int, host: str = "127.0.0.1") -> None:
        self.app = app
        self.host = host
        self.requested_port = port
        self.port: int | None = None
        self.error: str | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._server and self._server.started)

    def start(self, timeout: float = 10.0) -> int:
        import uvicorn

        with self._lock:
            if self.running and self.port:
                return self.port
            self.stop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, self.requested_port))
            except OSError as exc:
                sock.close()
                self.error = f"El puerto {self.requested_port} está ocupado ({exc.strerror or exc})."
                raise PublicMediaError("public_port_busy", self.error) from exc
            sock.listen(64)
            self.port = sock.getsockname()[1]
            config = uvicorn.Config(
                self.app,
                lifespan="off",
                log_level="warning",
                access_log=False,
                server_header=False,
                proxy_headers=False,
                ws="none",
                timeout_keep_alive=5,
                limit_concurrency=64,
            )
            server = uvicorn.Server(config)
            self._server = server

            def run() -> None:
                try:
                    server.run(sockets=[sock])
                except BaseException as exc:  # SystemExit from Uvicorn on startup failure
                    self.error = f"El servidor público se detuvo: {exc!r}"
                    log.exception("Public media server crashed")
                finally:
                    with contextlib.suppress(OSError):
                        sock.close()

            self._thread = threading.Thread(target=run, name="public-media-server", daemon=True)
            self._thread.start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if server.started:
                    self.error = None
                    log.info("Public media server listening on %s:%s", self.host, self.port)
                    return self.port
                if not self._thread.is_alive():
                    break
                time.sleep(0.02)
            self.error = self.error or "El servidor público no ha arrancado a tiempo."
            raise PublicMediaError("public_server_unavailable", self.error)

    def stop(self, timeout: float = 5.0) -> None:
        server, thread = self._server, self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._server = None
        self._thread = None


# ===================================================================================== cloudflared


def platform_asset(system: str | None = None, machine: str | None = None) -> str:
    """Release asset name for this OS/CPU. Raises :class:`PublicMediaError` when unsupported."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine() or "").lower()
    is_arm = machine in ("arm64", "aarch64", "armv8", "armv8l")
    is_64 = machine in ("amd64", "x86_64", "x64") or is_arm
    if system.startswith("win"):
        # No native arm64 build: x86-64 emulation runs the amd64 binary.
        return "cloudflared-windows-amd64.exe" if (is_64 or not machine) else "cloudflared-windows-386.exe"
    if system.startswith("linux"):
        if is_arm:
            return "cloudflared-linux-arm64"
        if machine in ("amd64", "x86_64"):
            return "cloudflared-linux-amd64"
    if system.startswith("darwin"):
        return "cloudflared-darwin-arm64.tgz" if is_arm else "cloudflared-darwin-amd64.tgz"
    raise PublicMediaError(
        "tunnel_unsupported",
        f"El túnel de Cloudflare no está disponible para este sistema ({system}/{machine}). "
        "Usa una URL pública propia.",
        transient=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pid_looks_like_cloudflared(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return "cloudflared" in out.lower()
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists():
            return b"cloudflared" in cmdline.read_bytes()
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5
        )
        return "cloudflared" in out.stdout
    except Exception:
        return False


class CloudflaredTunnel:
    """Downloads (pinned + verified) and runs ``cloudflared tunnel --url http://127.0.0.1:<port>``.

    ``command`` overrides the executable (tests run a fake ``cloudflared`` script with the same flags).
    """

    def __init__(
        self,
        bin_dir: Path,
        *,
        version: str = CLOUDFLARED_VERSION,
        checksums: Mapping[str, Mapping[str, str]] | None = None,
        asset: str | None = None,
        command: Sequence[str] | None = None,
        http_factory: Callable[[], httpx.Client] | None = None,
        hostname_timeout: float = HOSTNAME_TIMEOUT_S,
    ) -> None:
        self.bin_dir = Path(bin_dir)
        self.version = version
        self.checksums = checksums if checksums is not None else CLOUDFLARED_SHA256
        self._asset = asset
        self.command = list(command) if command else None
        self.http_factory = http_factory or (lambda: httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)))
        self.hostname_timeout = hostname_timeout
        self.proc: subprocess.Popen[bytes] | None = None
        self.hostname: str | None = None
        self.metrics_port: int | None = None
        self.state = "stopped"  # stopped | downloading | starting | running | error
        self.error: str | None = None
        self.started_at: datetime | None = None
        self.restarts = 0
        self._log_tail: deque[str] = deque(maxlen=50)
        self._verified_binary: str | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.RLock()

    # -- binary ------------------------------------------------------------------------------------

    @property
    def asset(self) -> str:
        return self._asset or platform_asset()

    @property
    def binary_path(self) -> Path:
        suffix = ".exe" if self.asset.endswith(".exe") else ""
        return self.bin_dir / f"cloudflared-{self.version}{suffix}"

    def expected_sha256(self) -> str:
        try:
            return self.checksums[self.version][self.asset]
        except KeyError:
            raise PublicMediaError(
                "cloudflared_unpinned",
                f"No hay suma SHA-256 fijada para cloudflared {self.version} ({self.asset}).",
                transient=False,
            ) from None

    @property
    def _marker(self) -> Path:
        return self.binary_path.with_name(self.binary_path.name + ".sha256")

    def binary_ready(self) -> bool:
        """The verified binary is on disk (checked against the marker written after verification)."""
        if self.command:
            return True
        path, marker = self.binary_path, self._marker
        try:
            expected = self.expected_sha256()
            lines = marker.read_text().split()
        except (OSError, PublicMediaError):
            return False
        if not path.is_file() or len(lines) != 2 or lines[0] != expected:
            return False
        if self._verified_binary == lines[1]:
            return True
        if _sha256_file(path) != lines[1]:
            log.warning("cloudflared binary changed on disk; it will be downloaded again")
            return False
        self._verified_binary = lines[1]
        return True

    def ensure_binary(self) -> Path:
        """Download + verify the pinned binary once. Only called when tunnel mode is enabled (consent)."""
        if self.command:
            return Path(self.command[0])
        path, marker = self.binary_path, self._marker
        if self.binary_ready():
            return path
        expected = self.expected_sha256()
        url = RELEASE_URL.format(version=self.version, asset=self.asset)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        part = self.bin_dir / f".{self.asset}.part"
        previous_state = self.state
        self.state = "downloading"
        log.info("Downloading cloudflared %s (%s)", self.version, self.asset)
        try:
            digest = hashlib.sha256()
            with self.http_factory() as http, http.stream("GET", url, follow_redirects=True) as response:
                if response.status_code != 200:
                    raise PublicMediaError(
                        "cloudflared_download",
                        f"No se pudo descargar cloudflared (HTTP {response.status_code}). Se reintentará.",
                    )
                with open(part, "wb") as fh:
                    for chunk in response.iter_bytes(1024 * 1024):
                        digest.update(chunk)
                        fh.write(chunk)
            if digest.hexdigest() != expected:
                raise PublicMediaError(
                    "cloudflared_checksum",
                    "El cloudflared descargado no coincide con la suma SHA-256 esperada y no se usará.",
                    transient=False,
                )
            if self.asset.endswith(".tgz"):
                self._extract_tgz(part, path)
            else:
                os.replace(part, path)
            with contextlib.suppress(OSError):
                path.chmod(0o755)
            binary_sha = _sha256_file(path)
            marker.write_text(f"{expected}\n{binary_sha}\n")
            self._verified_binary = binary_sha
            log.info("cloudflared %s ready at %s", self.version, path)
            return path
        except httpx.HTTPError as exc:
            raise PublicMediaError(
                "cloudflared_download", "No se pudo descargar cloudflared desde GitHub. Se reintentará."
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                part.unlink()
            if self.state == "downloading":
                self.state = previous_state

    @staticmethod
    def _extract_tgz(archive: Path, dest: Path) -> None:
        with tarfile.open(archive, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared"),
                None,
            )
            if member is None:
                raise PublicMediaError(
                    "cloudflared_archive",
                    "El archivo de cloudflared no contiene el ejecutable.",
                    transient=False,
                )
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - isfile() guarantees a stream
                raise PublicMediaError(
                    "cloudflared_archive", "Archivo de cloudflared ilegible.", transient=False
                )
            tmp = dest.with_name(dest.name + ".tmp")
            with source, open(tmp, "wb") as out:
                while chunk := source.read(1024 * 1024):
                    out.write(chunk)
            os.replace(tmp, dest)

    # -- process -----------------------------------------------------------------------------------

    @property
    def pid_file(self) -> Path:
        return self.bin_dir / "cloudflared.pid"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill_stale(self) -> None:
        """Kill a cloudflared left behind by a crashed previous run (PID file), if it still is one."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return
        if self.proc is not None and self.proc.pid == pid:
            return
        if pid > 0 and _pid_looks_like_cloudflared(pid):
            log.info("Stopping stale cloudflared (pid %s)", pid)
            with contextlib.suppress(Exception):
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    os.kill(pid, 15)
        with contextlib.suppress(OSError):
            self.pid_file.unlink()

    def start(self, target_port: int) -> str:
        """Start (or reuse) the tunnel to ``127.0.0.1:<target_port>``; returns the public hostname."""
        with self._lock:
            if self.alive and self.hostname:
                return self.hostname
            if self.proc is not None:
                self.restarts += 1
                self.stop()
            self.kill_stale()
            try:
                executable = self.ensure_binary()
            except PublicMediaError as exc:
                self.state, self.error = "error", exc.message
                raise
            self.state = "starting"
            self.error = None
            self.hostname = None
            self._log_tail.clear()
            self.metrics_port = _free_port()
            config_file = self.bin_dir / "cloudflared-vacio.yml"
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            if not config_file.exists():
                config_file.write_text("# Configuración vacía: el túnel rápido no usa ~/.cloudflared\n")
            args = [
                "tunnel",
                "--no-autoupdate",
                "--config",
                str(config_file),
                "--metrics",
                f"127.0.0.1:{self.metrics_port}",
                "--url",
                f"http://127.0.0.1:{target_port}",
            ]
            cmd = (self.command or [str(executable)]) + args
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.PIPE,
                "stdin": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            try:
                self.proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed argv, no shell
            except OSError as exc:
                self.state, self.error = "error", f"No se pudo ejecutar cloudflared: {exc}"
                raise PublicMediaError("tunnel_unavailable", MSG_TUNNEL) from exc
            with contextlib.suppress(OSError):
                self.pid_file.write_text(str(self.proc.pid))
            self._stderr_thread = threading.Thread(
                target=self._pump_stderr, args=(self.proc,), name="cloudflared-log", daemon=True
            )
            self._stderr_thread.start()
            hostname = self._wait_hostname()
            if hostname is None:
                tail = " | ".join(list(self._log_tail)[-3:])
                self.stop()
                self.state = "error"
                self.error = "cloudflared no ha dado una dirección pública." + (
                    f" Último mensaje: {tail}" if tail else ""
                )
                raise PublicMediaError("tunnel_unavailable", MSG_TUNNEL)
            self.hostname = hostname
            self.state = "running"
            self.started_at = _utcnow()
            log.info("Cloudflare quick tunnel up: https://%s", hostname)
            return hostname

    def _pump_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stderr
        if stream is None:
            return
        with contextlib.suppress(Exception):
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._log_tail.append(line[:300])
                    log.debug("cloudflared: %s", line[:300])

    def _hostname_from_logs(self) -> str | None:
        for line in reversed(self._log_tail):
            match = TUNNEL_HOST_RE.search(line)
            if match:
                return match.group(1)
        return None

    def _wait_hostname(self) -> str | None:
        deadline = time.monotonic() + self.hostname_timeout
        url = f"http://127.0.0.1:{self.metrics_port}/quicktunnel"
        with httpx.Client(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if not self.alive:
                    return None
                with contextlib.suppress(Exception):
                    response = client.get(url)
                    if response.status_code == 200:
                        host = str((response.json() or {}).get("hostname") or "").strip()
                        if host:
                            return host
                host = self._hostname_from_logs()
                if host:
                    return host
                time.sleep(0.2)
        return None

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            self.proc = None
            self.hostname = None
            if self.state != "error":
                self.state = "stopped"
            if proc is None:
                return
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                    proc.wait(timeout=5)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    proc.wait(timeout=5)
            with contextlib.suppress(Exception):
                if proc.stderr:
                    proc.stderr.close()
            with contextlib.suppress(OSError):
                self.pid_file.unlink()
            log.info("Cloudflare quick tunnel stopped")

    def status(self) -> dict[str, Any]:
        state = self.state
        if state == "running" and not self.alive:
            state = "error"
        asset: str | None
        try:
            asset = self.asset
        except PublicMediaError:
            asset = None
        return {
            "state": state,
            "hostname": self.hostname if state == "running" else None,
            "url": f"https://{self.hostname}" if state == "running" and self.hostname else None,
            "version": self.version,
            "asset": asset,
            "binary_present": bool(asset) and self.binary_ready(),
            "pid": self.proc.pid if self.alive and self.proc else None,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at and state == "running" else None,
            "restarts": self.restarts,
            "log_tail": list(self._log_tail)[-5:],
        }


# ========================================================================================== service


def _validate_base_url(value: Any) -> str | None:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        return None
    return text


@dataclass
class _Tracking:
    last_used: float = field(default_factory=time.monotonic)


class PublicMediaService:
    """Implements :class:`planificador.providers.base.PublicMedia` (SPEC §9).

    ``db_factory()`` returns a new Session (closed by the service); ``data_dir`` is the data root (only
    files inside it are ever served); ``settings_getter()`` returns a mapping with ``public_media_mode`` and
    ``public_media_base_url`` (read on every call so changes in Ajustes apply immediately).
    """

    def __init__(
        self,
        db_factory: Callable[[], Session],
        data_dir: Path | str,
        settings_getter: Callable[[], Mapping[str, Any]],
        *,
        port: int = DEFAULT_PORT,
        clock: Any = None,
        tunnel: CloudflaredTunnel | None = None,
        http_factory: Callable[[], httpx.Client] | None = None,
        verify_public: bool = True,
        idle_shutdown_s: float = IDLE_SHUTDOWN_S,
        watchdog_interval_s: float = WATCHDOG_INTERVAL_S,
        self_test_timeout_s: float = SELF_TEST_TIMEOUT_S,
    ) -> None:
        self.db_factory = db_factory
        self.data_dir = Path(data_dir).resolve()
        self.settings_getter = settings_getter
        self.port = int(port)
        self.clock = clock
        self.http_factory = http_factory or (lambda: httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0)))
        self.tunnel = tunnel or CloudflaredTunnel(self.data_dir / "bin", http_factory=http_factory)
        self.verify_public = verify_public
        self.idle_shutdown_s = idle_shutdown_s
        self.watchdog_interval_s = watchdog_interval_s
        self.self_test_timeout_s = self_test_timeout_s
        self.app = PublicMediaApp(db_factory, self.data_dir, self.now, on_hit=self._touch)
        self.server = PublicServer(self.app, self.port)
        self._tracking = _Tracking()
        self._lock = threading.RLock()
        self._watchdog: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._verified_host: str | None = None
        self.last_error: str | None = None
        atexit.register(self._atexit)

    # -- basics ------------------------------------------------------------------------------------

    def now(self) -> datetime:
        if self.clock is not None:
            return _aware(self.clock.now())
        return _utcnow()

    def _settings(self) -> Mapping[str, Any]:
        try:
            return self.settings_getter() or {}
        except Exception:
            log.exception("Public media settings unavailable")
            return {}

    def mode(self) -> str:
        mode = str(self._settings().get("public_media_mode") or "off")
        return mode if mode in MODES else "off"

    def _touch(self) -> None:
        self._tracking.last_used = time.monotonic()

    def _relative(self, path: Path | str) -> str:
        target = Path(path).resolve()
        if self.data_dir not in target.parents:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "public_media_path",
                "Solo se pueden publicar archivos de la biblioteca de medios.",
                detail={"name": target.name},
            )
        if not target.is_file():
            raise ProviderError(
                ErrorKind.PERMANENT,
                "media_missing",
                f"No se encuentra el archivo «{target.name}».",
                detail={"name": target.name},
            )
        return target.relative_to(self.data_dir).as_posix()

    # -- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        """Start the public server (and the watchdog) unless the mode is ``off``. The tunnel itself starts
        lazily on the first :meth:`url_for`."""
        if self.mode() == "off":
            return
        try:
            self.ensure_server()
        except PublicMediaError as exc:
            self.last_error = exc.message
            log.warning("Public media server not started: %s", exc.message)

    def stop(self) -> None:
        self._stop_event.set()
        self.tunnel.stop()
        self.server.stop()
        watchdog = self._watchdog
        if watchdog is not None and watchdog.is_alive() and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)
        self._watchdog = None
        self._verified_host = None

    def _atexit(self) -> None:
        with contextlib.suppress(Exception):
            self.tunnel.stop()

    def ensure_server(self) -> int:
        with self._lock:
            if not self.server.running:
                self.server.start()
            self._ensure_watchdog()
            return int(self.server.port or self.port)

    def _ensure_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._stop_event.clear()
        self._watchdog = threading.Thread(target=self._watch, name="public-media-watchdog", daemon=True)
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stop_event.wait(self.watchdog_interval_s):
            with contextlib.suppress(Exception):
                self.check_idle()

    def active_tokens(self) -> int:
        try:
            with self.db_factory() as session:
                return int(
                    session.scalar(
                        select(func.count())
                        .select_from(PublicMediaToken)
                        .where(
                            PublicMediaToken.revoked_at.is_(None), PublicMediaToken.expires_at > self.now()
                        )
                    )
                    or 0
                )
        except Exception:
            log.debug("Could not count public media tokens", exc_info=True)
            return 0

    def check_idle(self, monotonic_now: float | None = None) -> str:
        """Watchdog step: stop an idle tunnel, restart a dead one that is still needed, stop everything when
        the mode is switched off. Returns what it did (``idle_stop``/``restart``/``off``/``ok``)."""
        mode = self.mode()
        if mode == "off":
            if self.tunnel.alive or self.server.running:
                self.tunnel.stop()
                self.server.stop()
                return "off"
            return "ok"
        if mode != "tunnel":
            if self.tunnel.alive:
                self.tunnel.stop()
                return "idle_stop"
            return "ok"
        now = time.monotonic() if monotonic_now is None else monotonic_now
        active = self.active_tokens()
        if self.tunnel.alive:
            if active == 0 and now - self._tracking.last_used >= self.idle_shutdown_s:
                log.info("Public media idle for %.0f s: stopping the tunnel", now - self._tracking.last_used)
                self.tunnel.stop()
                self._verified_host = None
                return "idle_stop"
            return "ok"
        if active > 0 and self.tunnel.state in ("running", "error") and self.tunnel.restarts < 20:
            log.warning("Cloudflare tunnel died while tokens are active: restarting")
            with contextlib.suppress(PublicMediaError):
                self._tunnel_base()
                return "restart"
        return "ok"

    # -- public URL --------------------------------------------------------------------------------

    def prepare(self) -> dict[str, Any]:
        """Settings page hook after the user enables tunnel mode: download/verify the binary now."""
        if self.mode() == "tunnel":
            try:
                self.tunnel.ensure_binary()
            except PublicMediaError as exc:
                self.last_error = exc.message
        return self.status()

    def _tunnel_base(self) -> str:
        port = self.ensure_server()
        hostname = self.tunnel.start(port)
        base = f"https://{hostname}"
        if self.verify_public and self._verified_host != hostname:
            ok, message = self.probe_public(base)
            if not ok:
                # Fresh quick tunnels sometimes fail over QUIC; one clean restart usually fixes it.
                log.warning("Tunnel self-test failed (%s); restarting once", message)
                self.tunnel.stop()
                hostname = self.tunnel.start(port)
                base = f"https://{hostname}"
                ok, message = self.probe_public(base)
                if not ok:
                    self.tunnel.error = message
                    raise PublicMediaError("tunnel_unavailable", MSG_TUNNEL)
            self._verified_host = hostname
        return base

    def public_base(self) -> str:
        """``https://…`` base for the current mode (starts server/tunnel as needed)."""
        mode = self.mode()
        if mode == "off":
            raise PublicMediaError("public_media_off", MSG_OFF, transient=False)
        if mode == "base_url":
            base = _validate_base_url(self._settings().get("public_media_base_url"))
            if base is None:
                raise PublicMediaError("public_media_base_url_missing", MSG_BASE_URL, transient=False)
            self.ensure_server()
            return base
        return self._tunnel_base()

    def probe_public(self, base: str, timeout: float | None = None) -> tuple[bool, str]:
        """GET ``<base>/m/<random>`` until our own 404 (marker header) comes back through the public URL."""
        deadline = time.monotonic() + (self.self_test_timeout_s if timeout is None else timeout)
        url = f"{base.rstrip('/')}/m/{secrets.token_urlsafe(16)}"
        delay = 0.5
        message = "sin respuesta"
        with self.http_factory() as http:
            while True:
                try:
                    response = http.get(url, follow_redirects=False)
                    if response.status_code == 404 and response.headers.get(MARKER_HEADER) == "1":
                        return True, "La URL pública responde."
                    message = f"respuesta inesperada (HTTP {response.status_code})"
                except httpx.HTTPError as exc:
                    message = f"error de red ({type(exc).__name__})"
                if time.monotonic() + delay > deadline:
                    return False, f"La URL pública no responde: {message}."
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def url_for(self, path: Path, mime: str, ttl_seconds: int = DEFAULT_TTL_S) -> str:
        """Mint a 128-bit token for ``path`` and return ``https://<host>/m/<token>.<ext>``."""
        mode = self.mode()
        if mode == "off":
            raise ProviderError(ErrorKind.PERMANENT, "public_media_off", MSG_OFF)
        rel = self._relative(path)
        try:
            base = self.public_base()
        except PublicMediaError as exc:
            self.last_error = exc.message
            raise exc.to_provider_error() from exc
        ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL_S), MAX_TTL_S))
        token = secrets.token_urlsafe(16)
        now = self.now()
        with self.db_factory() as session:
            session.add(
                PublicMediaToken(
                    token=token,
                    rel_path=rel,
                    mime=mime or "application/octet-stream",
                    expires_at=now + timedelta(seconds=ttl),
                    created_at=now,
                    hits=0,
                )
            )
            session.commit()
        self._touch()
        self.last_error = None
        return f"{base}/m/{token}{_EXTENSIONS.get(mime, '')}"

    @staticmethod
    def token_from_url(url: str) -> str | None:
        match = TOKEN_RE.match(urlsplit(str(url or "")).path)
        return match.group(1) if match else None

    def release(self, url: str) -> None:
        """Revoke the token behind ``url`` (idempotent; unknown URLs are ignored)."""
        token = self.token_from_url(url)
        if token is None:
            return
        with self.db_factory() as session:
            session.execute(
                update(PublicMediaToken)
                .where(PublicMediaToken.token == token, PublicMediaToken.revoked_at.is_(None))
                .values(revoked_at=self.now())
            )
            session.commit()
        self._touch()

    def revoke_all(self) -> int:
        with self.db_factory() as session:
            count = session.execute(
                update(PublicMediaToken)
                .where(PublicMediaToken.revoked_at.is_(None))
                .values(revoked_at=self.now())
            ).rowcount
            session.commit()
        return int(count or 0)

    def test(self) -> dict[str, Any]:
        """ "Probar" button in Ajustes: bring the public URL up and check it from outside."""
        try:
            base = self.public_base()
        except PublicMediaError as exc:
            self.last_error = exc.message
            return {"ok": False, "message": exc.message, "status": self.status()}
        if self.mode() == "base_url" and self.verify_public:
            ok, message = self.probe_public(base)
        else:  # the tunnel was already checked from outside by public_base()
            ok, message = True, "La URL pública funciona."
        self.last_error = None if ok else message
        return {"ok": ok, "message": message, "base": base, "status": self.status()}

    def status(self) -> dict[str, Any]:
        """Snapshot for the settings page (JSON-friendly)."""
        mode = self.mode()
        settings = self._settings()
        tunnel = self.tunnel.status()
        public_base: str | None = None
        if mode == "base_url":
            public_base = _validate_base_url(settings.get("public_media_base_url"))
        elif mode == "tunnel":
            public_base = tunnel.get("url")
        labels = {
            "off": "Desactivada",
            "tunnel": "Túnel temporal de Cloudflare",
            "base_url": "URL pública propia",
        }
        return {
            "mode": mode,
            "mode_label": labels[mode],
            "enabled": mode != "off",
            "server": {
                "running": self.server.running,
                "port": self.server.port or self.port,
                "error": self.server.error,
            },
            "tunnel": tunnel,
            "base_url": _validate_base_url(settings.get("public_media_base_url")),
            "base_url_valid": _validate_base_url(settings.get("public_media_base_url")) is not None,
            "public_base": public_base,
            "active_tokens": self.active_tokens(),
            "idle_shutdown_s": self.idle_shutdown_s,
            "last_error": self.last_error,
        }


# ======================================================================================== app wiring

_service_lock = threading.Lock()


def _settings_reader(database: Any) -> Callable[[], Mapping[str, Any]]:
    def read() -> Mapping[str, Any]:
        from planificador import config

        with database.session() as session:
            return {
                "public_media_mode": config.get_setting(session, "public_media_mode"),
                "public_media_base_url": config.get_setting(session, "public_media_base_url"),
            }

    return read


def get_service(app: Any) -> PublicMediaService:
    """The app's :class:`PublicMediaService`, created on first use and stored in ``app.state.public_media``
    (``app`` may be the FastAPI app or its ``state``). Its ``stop`` is registered as a shutdown callback.
    Nothing is started here: the server/tunnel come up on the first :meth:`PublicMediaService.url_for`."""
    state = getattr(app, "state", app)
    existing = getattr(state, "public_media", None)
    if existing is not None:
        return existing
    with _service_lock:
        existing = getattr(state, "public_media", None)
        if existing is not None:
            return existing
        database = state.db
        settings = state.settings
        service = PublicMediaService(
            database.session,
            settings.data_dir,
            _settings_reader(database),
            port=getattr(settings, "pubmedia_port", None) or DEFAULT_PORT,
            clock=getattr(state, "clock", None),
        )
        state.public_media = service
        callbacks = getattr(state, "shutdown_callbacks", None)
        if isinstance(callbacks, list):
            callbacks.append(service.stop)
        return service


__all__ = [
    "CLOUDFLARED_SHA256",
    "CLOUDFLARED_VERSION",
    "CloudflaredTunnel",
    "PublicMediaApp",
    "PublicMediaError",
    "PublicMediaService",
    "PublicServer",
    "get_service",
    "platform_asset",
]

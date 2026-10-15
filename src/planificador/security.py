"""Local web security (SPEC §3).

- :class:`HostCheckMiddleware` (outermost): rejects any ``Host`` that is not ``localhost:<port>`` or
  ``127.0.0.1:<port>`` with 400 (DNS-rebinding defence). GET/HEAD requests on ``127.0.0.1`` are redirected to
  the canonical ``localhost`` origin so the session cookie (scoped to ``localhost``) is always sent.
- Starlette ``SessionMiddleware`` (signed cookie ``planificador_session``, HttpOnly, SameSite=Lax, path ``/``,
  secret derived from the master key in :mod:`planificador.crypto`).
- :class:`SecurityMiddleware` (inside the session middleware):
    * ensures a per-session CSRF token and exposes it as ``request.state.csrf_token`` (or call
      :func:`csrf_token`),
    * optional password: when set, every route except ``/login``, ``/static/*``, OAuth callbacks
      (``/conexiones/callback/*``), ``/healthz`` and ``/favicon.ico`` needs a logged-in session,
    * for POST/PUT/PATCH/DELETE: ``Sec-Fetch-Site`` must not be cross-site/same-site, ``Origin`` (or
      ``Referer`` when there is no Origin) must be our origin when present, and a valid CSRF token must come
      in the ``X-CSRF-Token`` header **or** the ``csrf_token`` field of an urlencoded form **or** the
      ``csrf_token`` field of a multipart form — which must come *before* any file field (put the hidden
      input first in the ``<form>`` / ``FormData``; only the start of the body is inspected so large uploads
      are never buffered) — **or** (multipart only) the ``csrf_token`` query parameter,
    * adds security headers (nosniff, frame deny, referrer policy, CSP).

Also provides scrypt password hashing and the ``/login`` ``/logout`` ``/api/csrf`` routes (:data:`router`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from markupsafe import escape
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

SESSION_COOKIE = "planificador_session"
SESSION_MAX_AGE = 7 * 24 * 3600
CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
AUTH_SESSION_KEY = "auth"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
MAX_FORM_BODY = 5 * 1024 * 1024
#: How much of a multipart body is scanned (and held in memory) looking for the ``csrf_token`` field.
MAX_MULTIPART_PREFIX = 1024 * 1024

#: Paths that never require the optional password.
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = ("/static/", "/conexiones/callback/")
AUTH_EXEMPT_PATHS: frozenset[str] = frozenset({"/login", "/healthz", "/favicon.ico", "/api/csrf"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "same-origin"),
    ("content-security-policy", CONTENT_SECURITY_POLICY),
)

# ------------------------------------------------------------------------------------- password hashing

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """``scrypt$N$r$p$salt$hash`` (salted, stdlib ``hashlib.scrypt``)."""
    if not password:
        raise ValueError("La contraseña no puede estar vacía.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not password or not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=256 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_fingerprint(stored: str | None) -> str | None:
    """Short fingerprint stored in the session: changing the password logs every session out."""
    if not stored:
        return None
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:24]


class _PasswordCache:
    """Caches the password hash for a few seconds so the middleware does not hit the DB per request.

    Keyed weakly by the ``Database`` object (not ``id()``, which can be reused by a new database once the old
    one is garbage collected — e.g. consecutive test apps — and would serve another database's hash)."""

    TTL = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: weakref.WeakKeyDictionary[Any, tuple[float, str | None]] = weakref.WeakKeyDictionary()

    def get(self, key: Any, loader: Callable[[], str | None]) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
        if cached is not None and now - cached[0] < self.TTL:
            return cached[1]
        value = loader()
        with self._lock:
            self._values[key] = (now, value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._values.clear()


_password_cache = _PasswordCache()


def get_password_hash(db: Session) -> str | None:
    from planificador.config import get_setting

    value = get_setting(db, "password_hash", None)
    return value if isinstance(value, str) and value else None


def password_enabled(db: Session) -> bool:
    return get_password_hash(db) is not None


def set_password(db: Session, password: str | None, *, commit: bool = True) -> None:
    """Set (or clear with ``None``/``""``) the optional app password. Existing sessions are logged out."""
    from planificador.config import set_setting

    value = hash_password(password) if password else None
    set_setting(db, "password_hash", value, commit=commit)
    _password_cache.invalidate()


def mark_logged_in(request: Request, db: Session) -> None:
    request.session[AUTH_SESSION_KEY] = password_fingerprint(get_password_hash(db))
    rotate_csrf_token(request)


def is_logged_in(session: dict[str, Any], stored_hash: str | None) -> bool:
    if stored_hash is None:
        return True
    fp = session.get(AUTH_SESSION_KEY)
    expected = password_fingerprint(stored_hash)
    return isinstance(fp, str) and expected is not None and hmac.compare_digest(fp, expected)


# ---------------------------------------------------------------------------------------------- CSRF


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token(request: Request) -> str:
    """The session's CSRF token (created on demand). Embed it in forms / ``hx-headers``."""
    token = getattr(request.state, "csrf_token", None)
    if token:
        return token
    if "session" not in request.scope:
        # No session middleware (isolated unit tests): a per-request token is the best we can do.
        token = _new_token()
    else:
        token = request.session.get(CSRF_SESSION_KEY)
        if not token:
            token = _new_token()
            request.session[CSRF_SESSION_KEY] = token
    request.state.csrf_token = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = _new_token()
    request.session[CSRF_SESSION_KEY] = token
    request.state.csrf_token = token
    return token


def tokens_match(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


# ----------------------------------------------------------------------------------------- middleware


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _send_plain(send: Send, status: int, text: str, headers: dict[str, str] | None = None) -> None:
    body = text.encode("utf-8")
    raw_headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
    ]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    await send({"type": "http.response.start", "status": status, "headers": raw_headers})
    await send({"type": "http.response.body", "body": body})


class HostCheckMiddleware:
    """Reject unexpected Host headers (400); redirect GET/HEAD on 127.0.0.1 to the canonical localhost."""

    def __init__(
        self, app: ASGIApp, allowed_hosts: Iterable[str], canonical_origin: str | None = None
    ) -> None:
        self.app = app
        self.allowed = frozenset(h.lower() for h in allowed_hosts)
        self.canonical_origin = canonical_origin.rstrip("/") if canonical_origin else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = (_header(scope, b"host") or "").strip().lower()
        if host not in self.allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await _send_plain(send, 400, "Host no permitido. Abre la aplicación en http://localhost.")
            return
        if (
            scope["type"] == "http"
            and self.canonical_origin
            and host.startswith("127.0.0.1")
            and scope.get("method") in ("GET", "HEAD")
        ):
            path = quote(scope.get("path", "/"), safe="/%:@!$&'()*+,;=-._~")
            qs = scope.get("query_string", b"").decode("latin-1")
            location = f"{self.canonical_origin}{path}" + (f"?{qs}" if qs else "")
            await _send_plain(send, 307, "Redirigiendo a localhost…", {"location": location})
            return
        await self.app(scope, receive, send)


class SecurityMiddleware:
    """CSRF + Origin checks, optional password gate and security headers (inside SessionMiddleware)."""

    def __init__(
        self,
        app: ASGIApp,
        allowed_origins: Iterable[str],
        password_hash_loader: Callable[[Scope], str | None] | None = None,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(o.lower().rstrip("/") for o in allowed_origins)
        self.password_hash_loader = password_hash_loader or _default_password_loader

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if session is None:  # pragma: no cover - misconfiguration
            raise RuntimeError("SecurityMiddleware must be installed inside SessionMiddleware")
        token = session.get(CSRF_SESSION_KEY)
        if not token:
            token = _new_token()
            session[CSRF_SESSION_KEY] = token
        scope.setdefault("state", {})["csrf_token"] = token

        send = self._with_headers(send)
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        # Optional password gate.
        if not _auth_exempt(path):
            stored = self.password_hash_loader(scope)
            if stored is not None and not is_logged_in(session, stored):
                await self._deny_auth(scope, send, method, path)
                return

        if method in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        # Fetch-metadata / Origin checks.
        problem = self._origin_problem(scope)
        if problem:
            log.warning("Blocked %s %s: %s", method, path, problem)
            await _send_plain(send, 403, "Petición bloqueada: origen no permitido.")
            return

        provided = _header(scope, CSRF_HEADER.encode())
        content_type = (_header(scope, b"content-type") or "").split(";")[0].strip().lower()
        if not provided and content_type == "application/x-www-form-urlencoded":
            body, too_large = await _read_body(receive, MAX_FORM_BODY)
            if too_large:
                await _send_plain(send, 413, "Formulario demasiado grande.")
                return
            receive = _replay(body, receive)
            try:
                fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            except ValueError:
                fields = {}
            provided = (fields.get(CSRF_FIELD) or [None])[0]
        if not provided and content_type == "multipart/form-data":
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            provided = (qs.get(CSRF_FIELD) or [None])[0]
            if not provided:
                provided, consumed = await _scan_multipart_for_token(
                    receive, _header(scope, b"content-type") or "", MAX_MULTIPART_PREFIX
                )
                receive = _replay_messages(consumed, receive)

        if not tokens_match(token, provided):
            log.warning("Blocked %s %s: missing or invalid CSRF token", method, path)
            await _send_plain(
                send,
                403,
                "Token de seguridad (CSRF) no válido o ausente. Recarga la página e inténtalo de nuevo.",
            )
            return

        await self.app(scope, receive, send)

    def _origin_problem(self, scope: Scope) -> str | None:
        site = (_header(scope, b"sec-fetch-site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return f"sec-fetch-site={site}"
        origin = _header(scope, b"origin")
        if origin is not None:
            origin = origin.strip().lower().rstrip("/")
            if origin not in self.allowed_origins:
                return f"origin={origin}"
            return None
        referer = _header(scope, b"referer")
        if referer:
            ref_origin = _origin_of(referer.strip())
            if ref_origin is None or ref_origin not in self.allowed_origins:
                return "referer mismatch"
        return None

    async def _deny_auth(self, scope: Scope, send: Send, method: str, path: str) -> None:
        is_htmx = (_header(scope, b"hx-request") or "").lower() == "true"
        qs = scope.get("query_string", b"").decode("latin-1")
        next_path = path + (f"?{qs}" if qs else "")
        login_url = "/login?next=" + quote(next_path, safe="")
        if is_htmx:
            await _send_plain(send, 401, "Inicia sesión.", {"hx-redirect": login_url})
        elif method in ("GET", "HEAD"):
            await _send_plain(send, 303, "Inicia sesión.", {"location": login_url})
        else:
            await _send_plain(send, 401, "Inicia sesión para continuar.")

    @staticmethod
    def _with_headers(send: Send) -> Callable[[Message], Awaitable[None]]:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        return wrapped


def _auth_exempt(path: str) -> bool:
    return path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES)


def _default_password_loader(scope: Scope) -> str | None:
    app = scope.get("app")
    database = getattr(getattr(app, "state", None), "db", None)
    if database is None:
        return None

    def load() -> str | None:
        with database.session() as s:
            return get_password_hash(s)

    return _password_cache.get(database, load)


async def _read_body(receive: Receive, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _replay_messages(messages: list[Message], receive: Receive) -> Receive:
    """Hand ``messages`` (already read from ``receive``) back to the app, then continue with ``receive``."""
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


def _multipart_parser_api() -> tuple[Any, Any] | None:
    try:
        from python_multipart.multipart import MultipartParser, parse_options_header
    except ImportError:  # pragma: no cover - very old python-multipart
        try:
            from multipart.multipart import MultipartParser, parse_options_header  # type: ignore[no-redef]
        except ImportError:
            return None
    return MultipartParser, parse_options_header


async def _scan_multipart_for_token(
    receive: Receive, content_type: str, limit: int
) -> tuple[str | None, list[Message]]:
    """Read the start of a multipart body looking for a plain ``csrf_token`` field.

    Stops at the first file part (the token must precede files, so uploads are never buffered), at the end of
    the body, or after ``limit`` bytes. Returns ``(token or None, messages consumed)``; the caller replays the
    messages so the endpoint still sees the whole body.
    """
    consumed: list[Message] = []
    api = _multipart_parser_api()
    if api is None:  # pragma: no cover
        return None, consumed
    parser_cls, parse_options = api
    _, params = parse_options(content_type)
    boundary = params.get(b"boundary")
    if not boundary:
        return None, consumed

    st: dict[str, Any] = {"done": False, "collect": False, "token": None}
    headers: dict[bytes, bytes] = {}
    field = bytearray()
    value = bytearray()
    data = bytearray()

    def on_part_begin() -> None:
        headers.clear()
        data.clear()
        st["collect"] = False

    def on_header_field(buf: bytes, start: int, end: int) -> None:
        field.extend(buf[start:end])

    def on_header_value(buf: bytes, start: int, end: int) -> None:
        value.extend(buf[start:end])

    def on_header_end() -> None:
        headers[bytes(field).strip().lower()] = bytes(value).strip()
        field.clear()
        value.clear()

    def on_headers_finished() -> None:
        _, opts = parse_options(headers.get(b"content-disposition", b""))
        if b"filename" in opts:
            st["done"] = True  # a file before the token: give up (never buffer uploads)
        st["collect"] = opts.get(b"name") == CSRF_FIELD.encode()

    def on_part_data(buf: bytes, start: int, end: int) -> None:
        if st["collect"] and not st["done"]:
            data.extend(buf[start:end])
            if len(data) > 1024:  # not a token
                st["done"] = True

    def on_part_end() -> None:
        if st["collect"] and not st["done"]:
            st["token"] = data.decode("utf-8", "replace")
            st["done"] = True

    def on_end() -> None:
        st["done"] = True

    parser = parser_cls(
        boundary,
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_headers_finished": on_headers_finished,
            "on_part_data": on_part_data,
            "on_part_end": on_part_end,
            "on_end": on_end,
        },
    )
    size = 0
    while not st["done"]:
        message = await receive()
        consumed.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if chunk:
            try:
                parser.write(chunk)
            except Exception:  # malformed body: no token (the endpoint reports its own parse error)
                break
        if not message.get("more_body", False) or size >= limit:
            break
    return st["token"], consumed


# ---------------------------------------------------------------------------------------------- routes

router = APIRouter(tags=["auth"])


def safe_next_path(value: str | None, default: str = "/") -> str:
    """A local redirect target from user input (``next`` fields): only same-origin absolute paths."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):  # no header injection / control characters
        return default
    return value


_safe_next = safe_next_path


def _login_page(request: Request, *, error: str | None, next_path: str, status_code: int = 200) -> Response:
    ctx = {"title": "Iniciar sesión", "error": error, "next": next_path, "csrf_token": csrf_token(request)}
    try:
        from planificador import templating

        response = templating.render(request, "login.html", **ctx)
        response.status_code = status_code
        return response
    except Exception as exc:  # template or templating module missing → minimal built-in page
        if not _is_template_missing(exc):
            raise
    err_html = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    html = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Iniciar sesión · Planificador</title>
<style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;
background:#f6f6fb;color:#1d1d29}}form{{background:#fff;padding:32px;border-radius:10px;
box-shadow:0 2px 12px rgba(0,0,0,.08);min-width:300px}}input,button{{width:100%;padding:10px;margin-top:8px;
box-sizing:border-box;border-radius:8px;border:1px solid #ccc}}button{{background:#5b4cf0;color:#fff;border:0;
cursor:pointer}}.error{{color:#c62828}}</style></head>
<body><form method="post" action="/login"><h1>Planificador</h1>{err_html}
<label for="password">Contraseña</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="{escape(next_path)}">
<input type="hidden" name="csrf_token" value="{escape(ctx["csrf_token"])}">
<button type="submit">Entrar</button></form></body></html>"""
    return HTMLResponse(html, status_code=status_code)


def _is_template_missing(exc: Exception) -> bool:
    from jinja2 import TemplateNotFound

    return isinstance(exc, ImportError | TemplateNotFound)


@router.get("/login", include_in_schema=False)
def login_form(request: Request, next: str = "/") -> Response:
    database = request.app.state.db
    with database.session() as db:
        if not password_enabled(db):
            return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, error=None, next_path=_safe_next(next))


@router.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    database = request.app.state.db
    with database.session() as db:
        stored = get_password_hash(db)
        if stored is None:
            return RedirectResponse(_safe_next(next), status_code=303)
        if not verify_password(password, stored):
            time.sleep(0.4)  # slow down guessing
            return _login_page(
                request, error="Contraseña incorrecta.", next_path=_safe_next(next), status_code=401
            )
        mark_logged_in(request, db)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", include_in_schema=False)
def logout(request: Request) -> Response:
    request.session.pop(AUTH_SESSION_KEY, None)
    rotate_csrf_token(request)
    database = request.app.state.db
    with database.session() as db:
        target = "/login" if password_enabled(db) else "/"
    if request.headers.get("hx-request") == "true":
        return PlainTextResponse("", headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@router.get("/api/csrf", include_in_schema=False)
def csrf_endpoint(request: Request) -> JSONResponse:
    """Current CSRF token (same-origin only: no CORS, Host-checked)."""
    return JSONResponse({"csrf_token": csrf_token(request)}, headers={"Cache-Control": "no-store"})

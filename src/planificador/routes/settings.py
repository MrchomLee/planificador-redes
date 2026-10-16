"""Ajustes (settings page).

Sections (each one its own form; htmx re-renders just that section, plain forms redirect back):

- **Credenciales** — Meta (App ID, App Secret, Graph API version, optional Login-for-Business
  ``config_id``), TikTok (Client Key, Client Secret, the scopes its app has — ``tiktok_scopes`` — and the PKCE
  encoding as an advanced option),
  Google/YouTube (client ID and secret). Secrets are **write-only**: the page only shows "•••• guardado"; an
  empty field keeps the stored value; they are stored encrypted (``config.set_setting(secret=True)``).
  ``POST /ajustes/credenciales/{meta|tiktok|google}``
- **Publicación** — default time zone (new brands), grace window for late posts, demo mode.
  ``POST /ajustes/publicacion``
- **URL pública para Instagram** — off / Cloudflare quick tunnel (explicit consent) / own public base URL,
  with the live status of :func:`planificador.publicmedia.get_service`. ``POST /ajustes/url-publica``,
  ``GET /ajustes/url-publica/estado``, ``POST /ajustes/url-publica/probar``
- **Seguridad** — optional password (set / change / remove). ``POST /ajustes/seguridad``
- **Avisos** — desktop and browser notifications. ``POST /ajustes/avisos``, ``POST /ajustes/avisos/probar``
- **Acerca de** — version, data folder, logs. ``GET /ajustes/registros``, ``POST /ajustes/abrir-carpeta``
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from planificador import __version__, config, crypto, security
from planificador.deps import ClockDep, DbSession
from planificador.models import Account, AccountStatus

log = logging.getLogger(__name__)

router = APIRouter()

SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("credenciales", "Credenciales", "key-round"),
    ("publicacion", "Publicación", "calendar-clock"),
    ("url-publica", "URL pública", "globe"),
    ("seguridad", "Seguridad", "lock"),
    ("avisos", "Avisos", "bell"),
    ("acerca", "Acerca de", "info"),
)

CREDENTIAL_GROUPS = ("meta", "tiktok", "google")
GROUP_NETWORKS = {"meta": ("facebook", "instagram"), "tiktok": ("tiktok",), "google": ("youtube",)}
GROUP_TITLES = {"meta": "Meta (Facebook e Instagram)", "tiktok": "TikTok", "google": "Google (YouTube)"}
SECRET_FIELDS = {
    "meta": "meta_app_secret",
    "tiktok": "tiktok_client_secret",
    "google": "google_client_secret",
}
ID_FIELDS = {"meta": "meta_app_id", "tiktok": "tiktok_client_key", "google": "google_client_id"}

#: Ajustes › Credenciales › TikTok scope checkboxes: (group title, [(scope, Spanish label, help)]). The first
#: one (``user.info.basic``) is always requested and shown ticked and disabled.
TIKTOK_SCOPE_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Siempre",
        (
            (
                "user.info.basic",
                "Iniciar sesión",
                "Tu nombre y tu foto de perfil. TikTok lo da a todas las apps.",
            ),
        ),
    ),
    (
        "Publicación (marca al menos uno)",
        (
            (
                "video.publish",
                "Publicación directa",
                "El vídeo se publica en tu perfil. En el portal: Content Posting API con «Direct Post» "
                "activado.",
            ),
            (
                "video.upload",
                "Enviar como borrador a la app de TikTok",
                "El vídeo llega a tu bandeja de TikTok y lo terminas allí.",
            ),
        ),
    ),
    (
        "Estadísticas (opcional)",
        (
            ("user.info.profile", "Perfil", "Tu @usuario y el enlace a tu perfil."),
            ("user.info.stats", "Estadísticas de la cuenta", "Seguidores, «me gusta» y número de vídeos."),
            (
                "video.list",
                "Estadísticas de tus vídeos",
                "Visualizaciones, «me gusta», comentarios y compartidos.",
            ),
        ),
    ),
)

GRAPH_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")
META_APP_ID_RE = re.compile(r"^\d{5,25}$")
CONFIG_ID_RE = re.compile(r"^\d{5,25}$")
TOKENISH_RE = re.compile(r"^[A-Za-z0-9._\-]{6,200}$")
GOOGLE_CLIENT_SUFFIX = ".apps.googleusercontent.com"
GRACE_MIN, GRACE_MAX = 0, 1440
PASSWORD_MIN = 6
LOG_TAIL_BYTES = 256 * 1024
LOG_TAIL_LINES = 400
TRANSITIONAL_TUNNEL_STATES = ("downloading", "starting")


# ------------------------------------------------------------------------------------------- helpers


def _render(request: Request, name: str, **ctx: Any) -> Response:
    from planificador.templating import render

    return render(request, name, **ctx)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    from planificador.templating import flash

    flash(request, message, kind)


def _toast_headers(message: str, kind: str = "success") -> dict[str, str]:
    from planificador.templating import toast_headers

    return toast_headers(message, kind)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _start_background(fn: Callable[[], None], name: str) -> None:
    """Run ``fn`` in a daemon thread (tests replace this to run it inline)."""
    threading.Thread(target=fn, name=name, daemon=True).start()


def _public_media_service(request: Request) -> Any:
    """The app's public media service (tests replace this with a fake)."""
    from planificador import publicmedia

    return publicmedia.get_service(request.app)


def _paths(request: Request) -> Any:
    paths = getattr(request.app.state, "paths", None)
    return paths if paths is not None else request.app.state.settings.paths


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _connected_count(db: Session, networks: tuple[str, ...]) -> int:
    rows = db.scalars(
        select(Account.id).where(Account.network.in_(networks), Account.status != AccountStatus.DISCONNECTED)
    ).all()
    return len(rows)


# ------------------------------------------------------------------------------------------- context


def credentials_context(request: Request, db: Session) -> dict[str, Any]:
    from planificador.routes.connections import redirect_uris

    ps = config.provider_settings(db, base_url=request.app.state.settings.base_url)
    uris = redirect_uris(ps)
    groups: dict[str, dict[str, Any]] = {}
    for group in CREDENTIAL_GROUPS:
        id_value = config.get_setting(db, ID_FIELDS[group], None) or ""
        secret_set = config.is_secret_set(db, SECRET_FIELDS[group])
        groups[group] = {
            "key": group,
            "title": GROUP_TITLES[group],
            "networks": GROUP_NETWORKS[group],
            "id_value": id_value,
            "secret_set": secret_set,
            "configured": bool(id_value and secret_set),
            "redirect_uri": uris[group],
            "connected": _connected_count(db, GROUP_NETWORKS[group]),
        }
    groups["meta"]["api_version"] = config.get_setting(db, "meta_api_version")
    groups["meta"]["config_id"] = config.get_setting(db, "meta_config_id", "") or ""
    groups["tiktok"]["pkce_encoding"] = ps.tiktok_pkce_encoding
    groups["tiktok"]["scopes"] = config.tiktok_scopes(db)
    groups["tiktok"]["scope_groups"] = TIKTOK_SCOPE_GROUPS
    return {"cred_groups": groups}


def publishing_context(db: Session) -> dict[str, Any]:
    from planificador.routes.brands import timezone_groups

    tz = config.default_timezone(db)
    return {
        "pub": {
            "default_timezone": tz,
            "grace_minutes": config.get_int(db, "grace_minutes"),
            "demo_mode": config.get_bool(db, "demo_mode"),
        },
        "tz_groups": timezone_groups(tz),
    }


def public_media_status(request: Request) -> dict[str, Any] | None:
    try:
        return _public_media_service(request).status()
    except Exception:  # noqa: BLE001 - the page must render anyway
        log.exception("Public media status unavailable")
        return None


def public_media_context(request: Request, db: Session) -> dict[str, Any]:
    mode = config.get_setting(db, "public_media_mode")
    if mode not in config.PUBLIC_MEDIA_MODES:
        mode = "off"
    status = public_media_status(request)
    tunnel_state = ((status or {}).get("tunnel") or {}).get("state")
    return {
        "pm": {
            "mode": mode,
            "base_url": config.get_setting(db, "public_media_base_url", "") or "",
            "consent_at": config.get_setting(db, "public_media_tunnel_consent_at", None),
            "public_port": request.app.state.settings.pubmedia_port,
            "status": status,
            "polling": mode == "tunnel" and tunnel_state in TRANSITIONAL_TUNNEL_STATES,
        }
    }


def security_context(db: Session) -> dict[str, Any]:
    return {"sec": {"password_set": security.password_enabled(db)}}


def notifications_context(db: Session) -> dict[str, Any]:
    return {
        "notif": {
            "desktop": config.get_bool(db, "notifications_desktop"),
            "browser": config.get_bool(db, "notifications_browser"),
        }
    }


def about_context(request: Request) -> dict[str, Any]:
    app_settings = request.app.state.settings
    paths = _paths(request)
    try:
        key_source = crypto.key_source()
    except Exception:  # noqa: BLE001
        key_source = "desconocido"
    return {
        "about": {
            "version": __version__,
            "data_dir": str(paths.root),
            "db_path": str(paths.db_path),
            "log_file": str(paths.log_file),
            "key_source": key_source,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}".strip(),
            "base_url": app_settings.base_url,
            "port": app_settings.port,
            "scheduler": bool(app_settings.scheduler_enabled),
        }
    }


def full_context(request: Request, db: Session) -> dict[str, Any]:
    ctx: dict[str, Any] = {"sections": SECTIONS, "errors": {}, "values": {}}
    ctx.update(credentials_context(request, db))
    ctx.update(publishing_context(db))
    ctx.update(public_media_context(request, db))
    ctx.update(security_context(db))
    ctx.update(notifications_context(db))
    ctx.update(about_context(request))
    return ctx


def _section_response(
    request: Request,
    db: Session,
    template: str,
    anchor: str,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, Any] | None = None,
    message: str | None = None,
    kind: str = "success",
) -> Response:
    """htmx: re-render the section (422 with errors). Plain form: flash + redirect, or the full page with
    the errors (422)."""
    errors = errors or {}
    if _is_htmx(request):
        ctx = full_context(request, db)
        ctx.update(errors=errors, values=values or {})
        response = _render(request, template, **ctx)
        if errors:
            response.status_code = 422
            response.headers.update(_toast_headers("Revisa los campos marcados.", "error"))
        elif message:
            response.headers.update(_toast_headers(message, kind))
        return response
    if errors:
        ctx = full_context(request, db)
        ctx.update(errors=errors, values=values or {}, page_title="Ajustes")
        response = _render(request, "settings/index.html", **ctx)
        response.status_code = 422
        return response
    if message:
        _flash(request, message, kind)
    return RedirectResponse(f"/ajustes#{anchor}", status_code=303)


# ---------------------------------------------------------------------------------------------- page


@router.get("/ajustes")
def settings_page(request: Request, db: DbSession) -> Response:
    ctx = full_context(request, db)
    return _render(request, "settings/index.html", page_title="Ajustes", **ctx)


# ------------------------------------------------------------------------------------------ credentials


def _validate_credentials(group: str, form: dict[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    ident = form.get("client_id", "")
    secret = form.get("client_secret", "")
    if group == "meta":
        if ident and not META_APP_ID_RE.match(ident):
            errors["client_id"] = (
                "El App ID de Meta es un número (lo ves en developers.facebook.com → tu app)."
            )
        if form.get("api_version") and not GRAPH_VERSION_RE.match(form["api_version"]):
            errors["api_version"] = "Usa el formato v25.0."
        if form.get("config_id") and not CONFIG_ID_RE.match(form["config_id"]):
            errors["config_id"] = "El ID de configuración es un número. Déjalo vacío si no lo usas."
    elif group == "tiktok":
        if ident and not TOKENISH_RE.match(ident):
            errors["client_id"] = "La Client Key solo tiene letras y números (sin espacios)."
        if form.get("pkce_encoding") not in config.PKCE_ENCODINGS:
            errors["pkce_encoding"] = "Elige hex o base64url."
    elif group == "google":
        if ident and not ident.endswith(GOOGLE_CLIENT_SUFFIX):
            errors["client_id"] = f"El ID de cliente de Google termina en «{GOOGLE_CLIENT_SUFFIX}»."
        elif ident and not TOKENISH_RE.match(ident.replace(GOOGLE_CLIENT_SUFFIX, "")):
            errors["client_id"] = "El ID de cliente no es válido (cópialo de nuevo desde Google Cloud)."
    if secret and (len(secret) < 6 or any(ch.isspace() for ch in secret) or len(secret) > 512):
        errors["client_secret"] = "El secreto no es válido (cópialo de nuevo, sin espacios)."
    return errors


def _tiktok_scope_selection(submitted: list[str] | None) -> tuple[list[str], str | None]:
    """(normalised scopes, error). Only known scopes; ``user.info.basic`` is always added; at least one of
    ``video.publish`` / ``video.upload`` is needed (without them nothing can be published)."""
    from planificador.providers import tiktok

    chosen = tiktok.normalize_scopes(
        [s for s in submitted or [] if s in tiktok.KNOWN_SCOPES], known_only=True
    )
    if not tiktok.PUBLISH_SCOPES & set(chosen):
        return chosen, (
            "Marca al menos «video.publish» (publicación directa) o «video.upload» (borrador en la app de "
            "TikTok): sin uno de los dos no se puede publicar en TikTok."
        )
    return chosen, None


@router.post("/ajustes/credenciales/{group}")
def save_credentials(
    request: Request,
    group: str,
    db: DbSession,
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
    clear_secret: Annotated[str, Form()] = "",
    api_version: Annotated[str, Form()] = "",
    config_id: Annotated[str, Form()] = "",
    pkce_encoding: Annotated[str, Form()] = "hex",
    scopes: Annotated[list[str] | None, Form()] = None,
    scopes_form: Annotated[str, Form()] = "",
) -> Response:
    if group not in CREDENTIAL_GROUPS:
        raise HTTPException(status_code=404)
    form = {
        "client_id": _clean(client_id),
        "client_secret": _clean(client_secret),
        "api_version": _clean(api_version),
        "config_id": _clean(config_id),
        "pkce_encoding": _clean(pkce_encoding) or "hex",
    }
    errors = {f"{group}_{k}": v for k, v in _validate_credentials(group, form).items()}
    values: dict[str, Any] = {f"{group}_{k}": v for k, v in form.items() if k != "client_secret"}
    new_scopes: list[str] | None = None
    if group == "tiktok" and scopes_form:  # the scope checkboxes were on the form (always, from the page)
        new_scopes, scope_error = _tiktok_scope_selection(scopes)
        values["tiktok_scopes"] = new_scopes
        if scope_error:
            errors["tiktok_scopes"] = scope_error
    template = "settings/_credentials_group_response.html"
    if errors:
        return _section_response(
            request, db, template, f"credenciales-{group}", errors=errors, values={**values, "group": group}
        )
    id_key, secret_key = ID_FIELDS[group], SECRET_FIELDS[group]
    previous_id = config.get_setting(db, id_key, None) or ""
    config.set_setting(db, id_key, form["client_id"] or None, commit=False)
    if clear_secret:
        config.set_setting(db, secret_key, None, secret=True, commit=False)
    elif form["client_secret"]:
        config.set_setting(db, secret_key, form["client_secret"], secret=True, commit=False)
    if group == "meta":
        config.set_setting(
            db, "meta_api_version", form["api_version"] or config.DEFAULTS["meta_api_version"], commit=False
        )
        config.set_setting(db, "meta_config_id", form["config_id"] or None, commit=False)
    scopes_changed = False
    if group == "tiktok":
        config.set_setting(db, "tiktok_pkce_encoding", form["pkce_encoding"], commit=False)
        if new_scopes is not None:
            scopes_changed = new_scopes != config.tiktok_scopes(db)
            config.set_setting(db, "tiktok_scopes", new_scopes, commit=False)
    db.commit()
    message, kind = f"Credenciales de {GROUP_TITLES[group]} guardadas.", "success"
    connected = _connected_count(db, GROUP_NETWORKS[group])
    if previous_id and previous_id != form["client_id"] and connected:
        message += (
            " Has cambiado de app: las cuentas conectadas con la anterior tendrán que reconectarse en "
            "Cuentas conectadas."
        )
        kind = "warning"
    elif scopes_changed and connected:
        message += (
            " Has cambiado los permisos: vuelve a conectar TikTok en Cuentas conectadas (botón «Reconectar») "
            "para que se apliquen."
        )
        kind = "warning"
    return _section_response(
        request, db, template, f"credenciales-{group}", values={"group": group}, message=message, kind=kind
    )


# ------------------------------------------------------------------------------------------- publishing


@router.post("/ajustes/publicacion")
def save_publishing(
    request: Request,
    db: DbSession,
    default_timezone: Annotated[str, Form()] = "",
    grace_minutes: Annotated[str, Form()] = "",
    demo_mode: Annotated[str, Form()] = "",
) -> Response:
    from planificador.routes.brands import is_known_timezone

    errors: dict[str, str] = {}
    tz = _clean(default_timezone)
    if not is_known_timezone(tz):
        errors["default_timezone"] = "Elige una zona horaria de la lista."
    try:
        grace = int(_clean(grace_minutes))
        if not GRACE_MIN <= grace <= GRACE_MAX:
            raise ValueError
    except ValueError:
        grace = 0
        errors["grace_minutes"] = f"Escribe un número de minutos entre {GRACE_MIN} y {GRACE_MAX}."
    values = {"default_timezone": tz, "grace_minutes": _clean(grace_minutes), "demo_mode": bool(demo_mode)}
    if errors:
        return _section_response(
            request, db, "settings/_publishing.html", "publicacion", errors=errors, values=values
        )
    was_demo = config.get_bool(db, "demo_mode")
    config.set_setting(db, "default_timezone", tz, commit=False)
    config.set_setting(db, "grace_minutes", grace, commit=False)
    config.set_setting(db, "demo_mode", bool(demo_mode), commit=False)
    db.commit()
    message = "Ajustes de publicación guardados."
    if bool(demo_mode) and not was_demo:
        message = (
            "Modo prueba activado: conecta la cuenta Demo en Cuentas conectadas para probar sin publicar."
        )
    elif was_demo and not demo_mode:
        message = "Modo prueba desactivado."
    if _is_htmx(request) and bool(demo_mode) != was_demo:
        # The demo ribbon lives in the layout: reload the page so it appears / disappears.
        _flash(request, message, "success")
        return Response(status_code=204, headers={"HX-Redirect": "/ajustes#publicacion"})
    return _section_response(request, db, "settings/_publishing.html", "publicacion", message=message)


# ----------------------------------------------------------------------------------------- public media


def validate_public_base_url(value: str) -> tuple[str | None, str | None]:
    """(normalised URL, error)."""
    text = _clean(value).rstrip("/")
    if not text:
        return None, "Escribe la dirección pública (https://…)."
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.hostname:
        return None, "La dirección debe empezar por https:// (Instagram solo descarga por HTTPS)."
    if parts.query or parts.fragment or parts.username or parts.password:
        return None, "Escribe solo la dirección base, sin «?», «#» ni usuario."
    if parts.hostname in ("localhost", "127.0.0.1") or parts.hostname.endswith(".local"):
        return None, "Tiene que ser una dirección accesible desde Internet, no localhost."
    return text, None


@router.post("/ajustes/url-publica")
def save_public_media(
    request: Request,
    db: DbSession,
    clock: ClockDep,
    mode: Annotated[str, Form()] = "off",
    consent: Annotated[str, Form()] = "",
    base_url: Annotated[str, Form()] = "",
) -> Response:
    errors: dict[str, str] = {}
    mode = mode if mode in config.PUBLIC_MEDIA_MODES else "off"
    previous = config.get_setting(db, "public_media_mode")
    values: dict[str, Any] = {"mode": mode, "base_url": _clean(base_url), "consent": bool(consent)}
    normalised: str | None = None
    if mode == "tunnel" and previous != "tunnel" and not consent:
        errors["consent"] = "Marca la casilla para confirmar que entiendes cómo funciona el túnel."
    if mode == "base_url":
        normalised, error = validate_public_base_url(base_url)
        if error:
            errors["base_url"] = error
    if errors:
        return _section_response(
            request, db, "settings/_public_media.html", "url-publica", errors=errors, values=values
        )
    config.set_setting(db, "public_media_mode", mode, commit=False)
    if mode == "tunnel" and consent:
        config.set_setting(db, "public_media_tunnel_consent_at", clock.now().isoformat(), commit=False)
    if mode == "base_url":
        config.set_setting(db, "public_media_base_url", normalised, commit=False)
    db.commit()
    service = None
    try:
        service = _public_media_service(request)
    except Exception:  # noqa: BLE001
        log.exception("Public media service unavailable")
    if service is not None and mode != previous:
        if mode == "tunnel":
            _start_background(service.prepare, "public-media-prepare")
        elif mode == "off":
            _start_background(service.stop, "public-media-stop")
    messages = {
        "off": "URL pública desactivada. Las fotos de Instagram no se podrán publicar automáticamente.",
        "tunnel": "Túnel temporal activado. Descargando y verificando cloudflared…"
        if mode != previous
        else "Ajustes de la URL pública guardados.",
        "base_url": "URL pública propia guardada. Pulsa «Probar» para comprobar que responde.",
    }
    return _section_response(
        request, db, "settings/_public_media.html", "url-publica", message=messages[mode]
    )


@router.get("/ajustes/url-publica/estado")
def public_media_state(request: Request, db: DbSession) -> Response:
    ctx = full_context(request, db)
    return _render(request, "settings/_pm_status_response.html", **ctx)


@router.post("/ajustes/url-publica/probar")
def test_public_media(request: Request, db: DbSession) -> Response:
    mode = config.get_setting(db, "public_media_mode")
    if mode not in ("tunnel", "base_url"):
        message, kind = "Activa primero el túnel temporal o tu URL pública propia.", "warning"
    else:
        try:
            result = _public_media_service(request).test()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            log.exception("Public media test failed")
            result = {"ok": False, "message": f"No se pudo probar ({type(exc).__name__})."}
        message = str(
            result.get("message") or ("La URL pública funciona." if result.get("ok") else "No responde.")
        )
        if result.get("ok") and result.get("base"):
            message = f"{message} ({result['base']})"
        kind = "success" if result.get("ok") else "error"
    if _is_htmx(request):
        ctx = full_context(request, db)
        response = _render(
            request, "settings/_pm_status_response.html", test_message=message, test_kind=kind, **ctx
        )
        response.headers.update(_toast_headers(message, kind))
        return response
    _flash(request, message, kind)
    return RedirectResponse("/ajustes#url-publica", status_code=303)


# --------------------------------------------------------------------------------------------- security


@router.post("/ajustes/seguridad")
def save_security(
    request: Request,
    db: DbSession,
    action: Annotated[str, Form()] = "set",
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
) -> Response:
    errors: dict[str, str] = {}
    stored = security.get_password_hash(db)
    if stored is not None and not security.verify_password(current_password, stored):
        errors["current_password"] = "La contraseña actual no es correcta."
    if action == "remove":
        if stored is None:
            return _section_response(
                request, db, "settings/_security.html", "seguridad", message="No había contraseña."
            )
        if errors:
            return _section_response(
                request,
                db,
                "settings/_security.html",
                "seguridad",
                errors=errors,
                values={"action": "remove"},
            )
        security.set_password(db, None)
        return _section_response(
            request,
            db,
            "settings/_security.html",
            "seguridad",
            message="Contraseña eliminada: la aplicación ya no pedirá contraseña.",
        )
    if len(new_password) < PASSWORD_MIN:
        errors["new_password"] = f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres."
    elif new_password != confirm_password:
        errors["confirm_password"] = "Las dos contraseñas no coinciden."
    if errors:
        return _section_response(request, db, "settings/_security.html", "seguridad", errors=errors)
    security.set_password(db, new_password)
    # Keep this browser logged in (the new password logs every other session out). This rotates the CSRF
    # token, so the page is reloaded.
    security.mark_logged_in(request, db)
    message = "Contraseña cambiada." if stored else "Contraseña activada: se pedirá al abrir la aplicación."
    _flash(request, message, "success")
    if _is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": "/ajustes#seguridad"})
    return RedirectResponse("/ajustes#seguridad", status_code=303)


# ---------------------------------------------------------------------------------------- notifications


@router.post("/ajustes/avisos")
def save_notifications(
    request: Request,
    db: DbSession,
    desktop: Annotated[str, Form()] = "",
    browser: Annotated[str, Form()] = "",
) -> Response:
    config.set_setting(db, "notifications_desktop", bool(desktop), commit=False)
    config.set_setting(db, "notifications_browser", bool(browser), commit=False)
    db.commit()
    return _section_response(
        request, db, "settings/_notifications.html", "avisos", message="Avisos guardados."
    )


@router.post("/ajustes/avisos/probar")
def test_notification(request: Request, db: DbSession, clock: ClockDep) -> Response:
    from planificador import notify

    desktop = config.get_bool(db, "notifications_desktop")
    notify.notify(
        db,
        "info",
        "Aviso de prueba",
        "Así verás los avisos de Planificador: publicaciones asistidas, errores y conexiones que renovar.",
        desktop=desktop,
        now=clock.now(),
    )
    message = "Aviso de prueba enviado: mira la campana" + (" y el escritorio." if desktop else ".")
    if _is_htmx(request):
        from planificador.templating import toast_headers

        headers = toast_headers(message, "success", **{"notifications:refresh": True})
        return Response(status_code=204, headers=headers)
    _flash(request, message, "success")
    return RedirectResponse("/ajustes#avisos", status_code=303)


# ---------------------------------------------------------------------------------------------- about


def read_log_tail(path: Path, max_lines: int = LOG_TAIL_LINES) -> list[str]:
    """Last lines of the log (scrubbed again, just in case)."""
    from planificador.providers.base import scrub_text

    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if len(data) >= LOG_TAIL_BYTES and lines:
        lines = lines[1:]  # first line is probably cut
    return [scrub_text(line) for line in lines[-max_lines:]]


@router.get("/ajustes/registros")
def show_logs(request: Request) -> Response:
    paths = _paths(request)
    lines = read_log_tail(paths.log_file)
    template = "settings/_logs_modal.html" if _is_htmx(request) else "settings/logs_page.html"
    return _render(request, template, page_title="Registros", log_lines=lines, log_file=str(paths.log_file))


def _open_in_file_manager(path: Path) -> None:
    """Open a folder in the OS file manager (local app: the server runs on the user's PC)."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603, S607


@router.post("/ajustes/abrir-carpeta")
def open_folder(request: Request, que: Annotated[str, Form()] = "datos") -> Response:
    paths = _paths(request)
    target = paths.logs_dir if que == "registros" else paths.root
    try:
        _open_in_file_manager(target)
        message, kind = f"Abriendo {target}", "info"
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open %s: %s", target, exc)
        message, kind = f"No se pudo abrir la carpeta. Está en: {target}", "warning"
    if _is_htmx(request):
        return Response(status_code=204, headers=_toast_headers(message, kind))
    _flash(request, message, kind)
    return RedirectResponse("/ajustes#acerca", status_code=303)

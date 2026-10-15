"""Instagram professional account provider via Facebook Login (SPEC §5, §7, §10; research
``docs/research/02-meta-api.md`` §4, §5.3–§5.4, §6.2, §11).

Container flow (``graph.facebook.com/{v}/{ig-user-id}``, Page token):

1. ``content_publishing_limit`` check (quota read live, §4.4).
2. One container per media item: images use ``image_url`` with a JPEG served through
   ``ctx.public_media.url_for`` (SPEC §9); videos use ``upload_type=resumable`` and the local bytes are sent
   to ``rupload.facebook.com/ig-api-upload/…`` with ``Authorization: OAuth`` (a failed upload gets a new
   container — never resumed mid-file, §4.3).
   ``post`` → IMAGE (1 image) / CAROUSEL (2–10 items, children first) / REELS (1 video, ``share_to_feed``);
   ``reel`` → REELS (``share_to_feed``, ``thumb_offset``, ``collaborators``, ``audio_name``, ``trial_params``,
   ``location_id``); ``story`` → STORIES (business accounts only, no caption).
3. Poll ``status_code`` with ``wait`` steps (5 s for the first minute, then 15 s): FINISHED → next;
   IN_PROGRESS → wait; ERROR → classified error; EXPIRED → new container.
4. ``ctx.before_commit`` → ``media_publish`` (the commit). Re-entry or reconcile reads the container first:
   ``PUBLISHED`` means it is live (never published twice).
5. Best-effort permalink, then the optional first comment (idempotent: look for our own comment before any
   retry; at most two attempts, §4.5/§11 #21), then the public URLs are released.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from planificador.providers import meta_common as mc
from planificador.providers.base import (
    AccountCreds,
    AccountSnapshot,
    CommentData,
    ErrorKind,
    FormatSpec,
    MediaInfo,
    OptionSpec,
    PostMetricData,
    Provider,
    ProviderError,
    ProviderSettings,
    PublishContext,
    ReconcileResult,
    StepResult,
    TokenUpdate,
)
from planificador.timeutil import Clock

log = logging.getLogger(__name__)

IMAGE_MAX_BYTES = 8_000_000  # §4.4: JPEG ≤ 8 MB
CAROUSEL_MIN = 2
CAROUSEL_MAX = 10  # §4.2: children 2–10
POLL_FAST_S = 5
POLL_SLOW_S = 15
FAST_WINDOW = timedelta(minutes=1)
IMAGE_PROCESSING_LIMIT = timedelta(minutes=5)
VIDEO_PROCESSING_LIMIT = timedelta(minutes=15)
CONTAINER_MAX_AGE = timedelta(hours=23)  # containers expire after 24 h (§4.4)
MAX_NOT_READY = 10
MAX_COMMENT_ATTEMPTS = 2
PUBLIC_URL_TTL = 3600

TRIAL_STRATEGIES = ("MANUAL", "SS_PERFORMANCE")

#: §5.3 account totals (2026: ``views`` replaces impressions; ``profile_views`` is deprecated).
ACCOUNT_TOTAL_METRICS = ["views", "total_interactions"]
MEDIA_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions"]
STORY_METRICS = ["views", "reach", "shares", "total_interactions"]
MEDIA_METRIC_KEYS = {
    "views": "views",
    "reach": "reach",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "saved": "saves",
    "total_interactions": "interactions",
}

_DIGITS = re.compile(r"\b(\d{4,7})\b")


def _now_iso(ctx: PublishContext) -> str:
    return mc.iso(ctx.clock.now())


def _age(ctx: PublishContext, when: str | None) -> timedelta:
    start = mc.parse_time(when)
    return ctx.clock.now() - start if start else timedelta(0)


def _collaborators(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list | tuple) else re.split(r"[,\s]+", str(value))
    out: list[str] = []
    for item in items:
        name = str(item or "").strip().lstrip("@").strip()
        if name and name not in out:
            out.append(name)
    return out


def _int_option(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class InstagramProvider(Provider):
    network = "instagram"
    display_name = "Instagram"
    formats = [
        FormatSpec("post", "Publicación", "images_or_video"),
        FormatSpec("reel", "Reel", "video"),
        FormatSpec("story", "Historia", "image_or_video"),
    ]
    supports_comments = True
    supports_analytics = True

    option_specs = [
        OptionSpec("first_comment", "Primer comentario", "text", None, formats=["post", "reel"]),
        OptionSpec("collaborators", "Colaboradores", "list", [], formats=["post", "reel"]),
        OptionSpec("share_to_feed", "Mostrar reel en el feed", "bool", True, formats=["reel"]),
        OptionSpec("thumb_offset_ms", "Portada (milisegundo del vídeo)", "number", None, formats=["reel"]),
        OptionSpec("audio_name", "Nombre del audio", "text", None, formats=["reel"]),
        OptionSpec("alt_text", "Texto alternativo", "text", None, formats=["post"]),
        OptionSpec(
            "trial_reel",
            "Reel de prueba",
            "select",
            None,
            choices=[("", "No"), ("MANUAL", "Sí, decidir yo"), ("SS_PERFORMANCE", "Sí, según rendimiento")],
            formats=["reel"],
        ),
        OptionSpec("location_id", "ID de ubicación", "text", None, formats=["post", "reel"]),
    ]

    # ------------------------------------------------------------------------------------- helpers

    @staticmethod
    def _ig(ctx: PublishContext) -> str:
        return ctx.account.external_id

    @staticmethod
    def _post(ctx: PublishContext, path: str, data: dict[str, Any]) -> dict:
        return mc.graph_post(ctx.http, ctx.settings, path, ctx.account.access_token, data)

    @staticmethod
    def _get(ctx: PublishContext, path: str, **params: Any) -> dict:
        return mc.graph_get(ctx.http, ctx.settings, path, ctx.account.access_token, **params)

    @staticmethod
    def _own_usernames(account: AccountCreds) -> set[str]:
        names = {
            str(account.meta.get("username") or "").lower().lstrip("@"),
            str(getattr(account, "username", None) or "").lower().lstrip("@"),
        }
        if account.name:
            names.add(account.name.lower().lstrip("@"))
        names.discard("")
        return names

    @staticmethod
    def _publish_container(state: dict) -> dict | None:
        if state.get("kind") == "CAROUSEL":
            return state.get("parent")
        items = state.get("items") or []
        return items[0] if items else None

    # ------------------------------------------------------------------------------------ planning

    def _plan(self, ctx: PublishContext) -> None:
        target = ctx.target
        media = target.media
        if any(m.kind not in ("image", "video") for m in media):
            raise ProviderError(ErrorKind.PERMANENT, "ig_media_kind", "Instagram solo admite fotos y vídeos.")
        fmt = target.format
        if fmt == "post":
            if not media:
                raise ProviderError(
                    ErrorKind.PERMANENT, "ig_no_media", "Instagram necesita al menos una foto o un vídeo."
                )
            if len(media) > CAROUSEL_MAX:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "ig_carousel_too_many",
                    f"Un carrusel de Instagram admite como máximo {CAROUSEL_MAX} archivos.",
                )
            if len(media) == 1:
                kind = "IMAGE" if media[0].kind == "image" else "REELS"
            else:
                kind = "CAROUSEL"
        elif fmt == "reel":
            if len(media) != 1 or media[0].kind != "video":
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "ig_reel_media",
                    "Un reel de Instagram necesita exactamente un vídeo.",
                )
            kind = "REELS"
        elif fmt == "story":
            if len(media) != 1:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "ig_story_media",
                    "Una historia de Instagram lleva una sola foto o un solo vídeo.",
                )
            account_type = (ctx.account.account_type or "").lower()
            if account_type and account_type not in ("business", "empresa"):
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "ig_not_business",
                    "Las historias de Instagram solo se pueden publicar desde cuentas de empresa (no de "
                    "creador).",
                )
            kind = "STORIES"
        else:
            raise ProviderError(ErrorKind.PERMANENT, "ig_format", "Formato de Instagram no disponible.")
        state = ctx.state
        state.update(
            {
                "v": 1,
                "kind": kind,
                "stage": "containers",
                "items": [{"i": index, "media": m.kind} for index, m in enumerate(media)],
                "public_urls": [],
            }
        )
        ctx.save_state(state)

    # ---------------------------------------------------------------------------------- publishing

    def publish_step(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        result = state.get("result")
        if result:
            return StepResult(
                "published",
                external_id=result.get("external_id"),
                external_url=result.get("external_url"),
                message=result.get("message"),
            )
        if "media_id" in state:
            return self._after_publish(ctx)
        if not state.get("kind"):
            self._plan(ctx)
        if state.get("stage") == "committing":
            # A media_publish was sent without a recorded outcome: read the container before anything else.
            check = self._remote_check(ctx)
            if check.outcome == "published":
                self._record_recovered(ctx, check)
                return self._after_publish(ctx)
            if check.outcome == "unknown":
                raise ProviderError(
                    ErrorKind.AMBIGUOUS,
                    "ig_commit_unknown",
                    "No se pudo confirmar si Instagram publicó el contenido. Revísalo en Instagram antes de "
                    "reintentar.",
                )
            mc.reopen_commit(ctx)

        if not state.get("quota_checked"):
            self._check_quota(ctx)
            state["quota_checked"] = True
            ctx.save_state(state)

        items: list[dict] = state["items"]
        media = ctx.target.media
        if len(media) != len(items):
            raise ProviderError(
                ErrorKind.PERMANENT,
                "ig_media_changed",
                "Los archivos de la publicación han cambiado; vuelve a programarla.",
            )
        self._expire_old_containers(ctx)

        for item in items:
            info = media[item["i"]]
            if not item.get("container_id"):
                self._create_item_container(ctx, item, info)
                return StepResult("continue", message="Preparando la publicación en Instagram…")
            if item["media"] == "video" and not item.get("uploaded"):
                self._upload_item(ctx, item, info)
                return StepResult("continue", message="Vídeo subido a Instagram.")

        verdict, wait = self._poll(ctx, items)
        if verdict != "ready":
            return self._poll_result(verdict, wait)

        if state["kind"] == "CAROUSEL":
            parent = state.get("parent") or {}
            if not parent.get("container_id"):
                self._create_parent(ctx)
                return StepResult("continue", message="Preparando el carrusel en Instagram…")
            verdict, wait = self._poll(ctx, [parent])
            if verdict != "ready":
                return self._poll_result(verdict, wait)

        publish_container = self._publish_container(state) or {}
        if publish_container.get("status") == "PUBLISHED":
            # Already live (e.g. an earlier attempt whose outcome was lost): never publish twice.
            media_id, permalink = self._find_published_media(ctx)
            self._record_recovered(
                ctx, ReconcileResult("published", external_id=media_id, external_url=permalink)
            )
            return self._after_publish(ctx)

        self._release_urls(ctx)
        return self._publish(ctx)

    @staticmethod
    def _poll_result(verdict: str, wait: int | None) -> StepResult:
        if verdict == "continue":
            return StepResult("continue")
        return StepResult(
            "wait", wait_seconds=wait or POLL_FAST_S, message="Instagram está procesando el contenido…"
        )

    # -- containers --------------------------------------------------------------------------------

    def _public_url(self, ctx: PublishContext, info: MediaInfo) -> str:
        if ctx.public_media is None:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "public_media_off",
                "Para publicar fotos en Instagram activa la URL pública en Ajustes.",
            )
        path, _mime = mc.prepare_image(info, IMAGE_MAX_BYTES, accept=("image/jpeg",), prefer_derived=True)
        return ctx.public_media.url_for(path, "image/jpeg", ttl_seconds=PUBLIC_URL_TTL)

    def _common_fields(
        self, ctx: PublishContext, *, caption: bool, collaborators: bool, location: bool
    ) -> dict:
        options = ctx.target.options or {}
        data: dict[str, Any] = {}
        if caption and (ctx.target.text or "").strip():
            data["caption"] = ctx.target.text
        if collaborators:
            names = _collaborators(options.get("collaborators"))
            if names:
                data["collaborators"] = names
        if location:
            location_id = str(options.get("location_id") or "").strip()
            if location_id:
                data["location_id"] = location_id
        return data

    def _create_item_container(self, ctx: PublishContext, item: dict, info: MediaInfo) -> None:
        state = ctx.state
        kind = state["kind"]
        options = ctx.target.options or {}
        data: dict[str, Any] = {}
        if info.kind == "image":
            url = item.get("public_url")
            if not url:
                url = self._public_url(ctx, info)
                item["public_url"] = url
                state.setdefault("public_urls", []).append(url)
                ctx.save_state(state)  # persisted before use so it is always released
            data["image_url"] = url
        else:
            data["upload_type"] = "resumable"

        if kind == "CAROUSEL":
            data["is_carousel_item"] = True
            if info.kind == "video":
                data["media_type"] = "VIDEO"
        elif kind == "IMAGE":
            data.update(self._common_fields(ctx, caption=True, collaborators=True, location=True))
            alt_text = str(options.get("alt_text") or "").strip()
            if alt_text:
                data["alt_text"] = alt_text
        elif kind == "REELS":
            data["media_type"] = "REELS"
            data.update(self._common_fields(ctx, caption=True, collaborators=True, location=True))
            share = options.get("share_to_feed")
            data["share_to_feed"] = True if ctx.target.format == "post" or share is None else bool(share)
            thumb = _int_option(options.get("thumb_offset_ms"))
            if thumb is not None:
                data["thumb_offset"] = thumb
            audio_name = str(options.get("audio_name") or "").strip()
            if audio_name:
                data["audio_name"] = audio_name
            trial = str(options.get("trial_reel") or "").strip().upper()
            if ctx.target.format == "reel" and trial in TRIAL_STRATEGIES and not state.get("trial_dropped"):
                data["trial_params"] = {"graduation_strategy": trial}
        elif kind == "STORIES":
            data["media_type"] = "STORIES"

        try:
            body = self._post(ctx, f"{self._ig(ctx)}/media", data)
        except ProviderError as exc:
            if exc.code == "ig_trial_reel_unavailable" and "trial_params" in data:
                state["trial_dropped"] = True
                ctx.save_state(state)
                ctx.log(
                    "ig_trial_dropped",
                    "Instagram no permite un reel de prueba en esta cuenta: se publicará como reel normal.",
                    exc.detail,
                )
                return
            if exc.kind in (ErrorKind.PERMANENT, ErrorKind.AUTH):
                self._release_urls(ctx)
            raise
        container_id = body.get("id")
        if not container_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "ig_no_id", "Instagram no devolvió el identificador del contenedor."
            )
        item.update(
            {
                "container_id": str(container_id),
                "uri": body.get("uri"),
                "created_at": _now_iso(ctx),
                "status": None,
                "uploaded": info.kind == "image",
            }
        )
        ctx.save_state(state)
        ctx.log(
            "ig_container_created", "Contenedor de Instagram creado.", {"container_id": str(container_id)}
        )

    def _upload_item(self, ctx: PublishContext, item: dict, info: MediaInfo) -> None:
        state = ctx.state
        uri = item.get("uri") or (
            f"https://{mc.RUPLOAD_HOST}/ig-api-upload/{mc.api_version(ctx.settings)}/{item['container_id']}"
        )
        try:
            mc.rupload(ctx.http, uri, ctx.account.access_token, info.path, prefixes=("/ig-api-upload/",))
        except ProviderError:
            # ig-publish never resumes mid-file: the next attempt uses a fresh container (§4.3 gotchas).
            self._reset_container(state, item)
            ctx.save_state(state)
            raise
        item["uploaded"] = True
        item["created_at"] = _now_iso(ctx)  # processing starts now
        ctx.save_state(state)
        ctx.log(
            "ig_video_uploaded",
            "Vídeo subido a Instagram; procesando.",
            {"container_id": item["container_id"]},
        )

    def _create_parent(self, ctx: PublishContext) -> None:
        state = ctx.state
        children = [str(item["container_id"]) for item in state["items"]]
        if not CAROUSEL_MIN <= len(children) <= CAROUSEL_MAX:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "ig_carousel_size",
                f"Un carrusel de Instagram necesita entre {CAROUSEL_MIN} y {CAROUSEL_MAX} archivos.",
            )
        data = {
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            **self._common_fields(ctx, caption=True, collaborators=True, location=True),
        }
        body = self._post(ctx, f"{self._ig(ctx)}/media", data)
        container_id = body.get("id")
        if not container_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "ig_no_id", "Instagram no devolvió el identificador del carrusel."
            )
        state["parent"] = {
            "container_id": str(container_id),
            "created_at": _now_iso(ctx),
            "status": None,
            "media": "parent",
        }
        ctx.save_state(state)
        ctx.log("ig_carousel_created", "Carrusel de Instagram creado.", {"container_id": str(container_id)})

    @staticmethod
    def _reset_container(state: dict, container: dict) -> None:
        for key in ("container_id", "uri", "uploaded", "status", "created_at"):
            container.pop(key, None)
        if container.get("media") != "parent" and state.get("kind") == "CAROUSEL":
            state.pop("parent", None)  # the parent references the old child
        if container.get("media") == "parent":
            state.pop("parent", None)

    def _reset_all(self, state: dict) -> None:
        for item in state.get("items") or []:
            self._reset_container(state, item)
        state.pop("parent", None)
        state.pop("commit_cid", None)

    def _expire_old_containers(self, ctx: PublishContext) -> None:
        state = ctx.state
        containers = list(state.get("items") or [])
        if state.get("parent"):
            containers.append(state["parent"])
        expired = False
        for container in containers:
            if container.get("container_id") and _age(ctx, container.get("created_at")) > CONTAINER_MAX_AGE:
                self._reset_container(state, container)
                expired = True
        if expired:
            ctx.save_state(state)
            ctx.log("ig_container_renewed", "El contenedor de Instagram era antiguo; se creará otro.", None)

    def _poll(self, ctx: PublishContext, containers: list[dict]) -> tuple[str, int | None]:
        """("ready", None) | ("wait", seconds) | ("continue", None) after polling ``status_code``."""
        state = ctx.state
        pending: list[timedelta] = []
        changed = False
        for container in containers:
            if container.get("status") in ("FINISHED", "PUBLISHED"):
                continue
            body = self._get(ctx, container["container_id"], fields="status_code,status")
            code = str(body.get("status_code") or "").upper()
            if code in ("FINISHED", "PUBLISHED"):
                container["status"] = code
                changed = True
                continue
            if code == "EXPIRED":
                self._reset_container(state, container)
                ctx.save_state(state)
                ctx.log("ig_container_expired", "El contenedor de Instagram caducó; se creará otro.", None)
                return "continue", None
            if code == "ERROR":
                self._container_error(ctx, container, str(body.get("status") or ""))
            age = _age(ctx, container.get("created_at"))
            limit = (
                VIDEO_PROCESSING_LIMIT
                if container.get("media") in ("video", "parent")
                else IMAGE_PROCESSING_LIMIT
            )
            if age > limit:
                self._reset_container(state, container)
                ctx.save_state(state)
                raise ProviderError(
                    ErrorKind.TRANSIENT,
                    "ig_processing_timeout",
                    "Instagram tardó demasiado en procesar el archivo. Se reintentará con un contenedor "
                    "nuevo.",
                )
            pending.append(age)
        if changed:
            ctx.save_state(state)
        if pending:
            return "wait", POLL_FAST_S if min(pending) < FAST_WINDOW else POLL_SLOW_S
        return "ready", None

    def _container_error(self, ctx: PublishContext, container: dict, status_text: str) -> None:
        state = ctx.state
        match = _DIGITS.search(status_text or "")
        subcode = int(match.group(1)) if match else None
        detail = {"container_status": mc.scrub_text(status_text)[:300], "subcode": subcode}
        self._reset_container(state, container)
        if subcode in mc.SUBCODE_MAP:
            kind, code, message, retry = mc.SUBCODE_MAP[subcode]
        else:
            shown = status_text.strip()[:200]
            kind, code, retry = ErrorKind.PERMANENT, "ig_container_error", None
            message = "Instagram no pudo procesar el archivo" + (f" ({shown})." if shown else ".")
        if kind == ErrorKind.TRANSIENT:
            ctx.save_state(state)
        else:
            self._release_urls(ctx)
        raise ProviderError(kind, code, message, detail=detail, retry_after=retry)

    # -- commit ------------------------------------------------------------------------------------

    def _publish(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        container = self._publish_container(state) or {}
        container_id = container.get("container_id")
        if not container_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "ig_bad_state", "Falta el contenedor de Instagram; se volverá a crear."
            )
        state["commit_cid"] = container_id
        state["stage"] = "commit"
        ig = self._ig(ctx)
        try:
            body = mc.run_commit(
                ctx,
                "Instagram",
                lambda: self._post(ctx, f"{ig}/media_publish", {"creation_id": container_id}),
                sent={"caption": ctx.target.text or ""},
            )
        except ProviderError as exc:
            if exc.code == "ig_media_not_ready":
                count = int(state.get("not_ready") or 0) + 1
                state["not_ready"] = count
                ctx.save_state(state)
                if count <= MAX_NOT_READY:
                    return StepResult(
                        "wait", wait_seconds=exc.retry_after or 30, message="Instagram aún está procesando…"
                    )
            if exc.code in ("ig_container_invalid", "ig_container_expired", "ig_upload_failed"):
                self._reset_all(state)
                ctx.save_state(state)
            elif exc.kind in (ErrorKind.PERMANENT, ErrorKind.AUTH):
                self._release_urls(ctx)
            raise
        media_id = body.get("id")
        if not media_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS, "ig_no_id", "Instagram no devolvió el identificador de la publicación."
            )
        state["media_id"] = str(media_id)
        state["stage"] = "published"
        state.pop("commit_rejected", None)
        ctx.save_state(state)
        ctx.log("ig_published", "Publicado en Instagram.", {"media_id": str(media_id)})
        return self._after_publish(ctx)

    def _after_publish(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        media_id = state.get("media_id") or None
        if media_id and not state.get("permalink_checked"):
            try:
                body = self._get(ctx, media_id, fields="permalink")
                state["permalink"] = body.get("permalink")
            except ProviderError as exc:
                log.info("Instagram permalink lookup failed: %s", exc.code)  # never fail a live post (§4.1)
            state["permalink_checked"] = True
            ctx.save_state(state)
        first_comment = str((ctx.target.options or {}).get("first_comment") or "").strip()
        if media_id and first_comment and state.get("kind") != "STORIES":
            self._first_comment(ctx, media_id, first_comment)
        self._release_urls(ctx)
        message = "Publicado en Instagram."
        if (state.get("first_comment") or {}).get("status") == "failed":
            message = "Publicado en Instagram (no se pudo añadir el primer comentario)."
        state["stage"] = "done"
        state["result"] = {
            "external_id": media_id,
            "external_url": state.get("permalink"),
            "message": message,
        }
        ctx.save_state(state)
        return StepResult(
            "published", external_id=media_id, external_url=state.get("permalink"), message=message
        )

    # -- first comment -----------------------------------------------------------------------------

    def _find_own_comment(self, ctx: PublishContext, media_id: str, text: str) -> str | None:
        wanted = mc.normalize_text(text)
        own_names = self._own_usernames(ctx.account)
        ig = self._ig(ctx)
        params = {
            **mc.auth_params(ctx.settings, ctx.account.access_token),
            "fields": "id,text,username,from{id,username}",
            "limit": "50",
        }
        try:
            for comment in mc.iter_paginated(
                ctx.http, mc.graph_url(ctx.settings, f"{media_id}/comments"), params, max_pages=2
            ):
                if mc.normalize_text(comment.get("text")) != wanted:
                    continue
                author = comment.get("from") or {}
                username = str(comment.get("username") or author.get("username") or "").lower()
                if not author and not username:
                    return str(comment.get("id"))
                if str(author.get("id") or "") == ig or (username and username in own_names):
                    return str(comment.get("id"))
        except ProviderError as exc:
            log.info("Could not list Instagram comments: %s", exc.code)
        return None

    def _first_comment(self, ctx: PublishContext, media_id: str, text: str) -> None:
        """Post the first comment at most twice, looking for it before any retry (§4.5, §11 #21)."""
        state = ctx.state
        fc = dict(state.get("first_comment") or {"status": "pending", "attempts": 0})
        if fc.get("status") in ("done", "failed"):
            return
        if fc.get("status") == "sending":
            found = self._find_own_comment(ctx, media_id, text)
            if found:
                fc.update({"status": "done", "id": found})
                state["first_comment"] = fc
                ctx.save_state(state)
                return
        while int(fc.get("attempts") or 0) < MAX_COMMENT_ATTEMPTS:
            fc["status"] = "sending"
            fc["attempts"] = int(fc.get("attempts") or 0) + 1
            state["first_comment"] = fc
            ctx.save_state(state)
            try:
                body = self._post(ctx, f"{media_id}/comments", {"message": text})  # never pass `fields`
            except ProviderError as exc:
                definite = exc.kind == ErrorKind.AUTH or (
                    exc.kind == ErrorKind.PERMANENT and exc.code != "ig_comment_failed"
                )
                if definite:
                    fc.update({"status": "failed", "error": exc.message})
                    break
                found = self._find_own_comment(ctx, media_id, text)
                if found:
                    fc.update({"status": "done", "id": found})
                    state["first_comment"] = fc
                    ctx.save_state(state)
                    ctx.log(
                        "ig_first_comment", "Primer comentario publicado en Instagram.", {"comment_id": found}
                    )
                    return
                fc["error"] = exc.message
                continue
            fc.update({"status": "done", "id": str(body.get("id") or "")})
            state["first_comment"] = fc
            ctx.save_state(state)
            ctx.log("ig_first_comment", "Primer comentario publicado en Instagram.", {"comment_id": fc["id"]})
            return
        fc["status"] = "failed"
        state["first_comment"] = fc
        ctx.save_state(state)
        ctx.log(
            "ig_first_comment_failed",
            "La publicación salió, pero no se pudo añadir el primer comentario.",
            {"error": fc.get("error")},
        )

    # -- misc --------------------------------------------------------------------------------------

    def _check_quota(self, ctx: PublishContext) -> None:
        """``content_publishing_limit`` (100 API posts / rolling 24 h — read live, never hard-coded, §4.4)."""
        try:
            body = self._get(ctx, f"{self._ig(ctx)}/content_publishing_limit", fields="config,quota_usage")
        except ProviderError as exc:
            if exc.kind in (ErrorKind.AUTH, ErrorKind.RATE_LIMITED):
                raise
            log.info("Instagram publishing limit unavailable: %s", exc.code)
            return
        data = (body.get("data") or [{}])[0] if isinstance(body.get("data"), list) else body
        data = data or {}
        usage = data.get("quota_usage")
        total = (data.get("config") or {}).get("quota_total")
        try:
            usage_n, total_n = int(usage), int(total)
        except (TypeError, ValueError):
            return
        ctx.state["quota"] = {"usage": usage_n, "total": total_n}
        if total_n and usage_n >= total_n:
            raise ProviderError(
                ErrorKind.RATE_LIMITED,
                "ig_quota_reached",
                f"Has alcanzado el límite de Instagram ({usage_n} de {total_n} publicaciones por API en 24 "
                "horas). Se reintentará más tarde.",
                detail={"quota_usage": usage_n, "quota_total": total_n},
                retry_after=mc.QUOTA_WAIT,
            )

    def _release_urls(self, ctx: PublishContext) -> None:
        state = ctx.state
        urls = list(state.get("public_urls") or [])
        if not urls:
            return
        if ctx.public_media is not None:
            for url in urls:
                try:
                    ctx.public_media.release(url)
                except Exception:  # best effort
                    log.warning("Could not release a public media URL", exc_info=True)
        state["public_urls"] = []
        for item in state.get("items") or []:
            item.pop("public_url", None)
        ctx.save_state(state)

    # ------------------------------------------------------------------------------------ reconcile

    def _commit_time(self, ctx: PublishContext) -> datetime:
        return mc.parse_time(ctx.state.get("commit_at")) or ctx.clock.now()

    def _find_published_media(self, ctx: PublishContext) -> tuple[str | None, str | None]:
        state = ctx.state
        since = self._commit_time(ctx) - timedelta(minutes=10)
        wanted = mc.normalize_text((state.get("sent") or {}).get("caption", ctx.target.text))
        edge = "stories" if state.get("kind") == "STORIES" else "media"
        params = {
            **mc.auth_params(ctx.settings, ctx.account.access_token),
            "fields": "id,caption,timestamp,permalink",
            "limit": "10",
        }
        try:
            for item in mc.iter_paginated(
                ctx.http, mc.graph_url(ctx.settings, f"{self._ig(ctx)}/{edge}"), params, max_pages=1
            ):
                stamp = mc.parse_time(item.get("timestamp"))
                if stamp is not None and stamp < since:
                    continue
                if edge == "stories" or mc.normalize_text(item.get("caption")) == wanted:
                    return str(item.get("id")), item.get("permalink")
        except ProviderError as exc:
            log.info("Could not look up the published Instagram media: %s", exc.code)
        return None, None

    def _remote_check(self, ctx: PublishContext) -> ReconcileResult:
        state = ctx.state
        container_id = state.get("commit_cid")
        if not container_id:
            return ReconcileResult("unknown", message="No hay constancia del contenedor enviado a Instagram.")
        body = self._get(ctx, container_id, fields="status_code,status")
        code = str(body.get("status_code") or "").upper()
        if code == "PUBLISHED":
            media_id, permalink = self._find_published_media(ctx)
            return ReconcileResult(
                "published",
                external_id=media_id,
                external_url=permalink,
                message="Instagram confirma la publicación.",
            )
        if code in ("FINISHED", "IN_PROGRESS"):
            return ReconcileResult("not_published", message="Instagram no llegó a publicar el contenido.")
        if code in ("ERROR", "EXPIRED"):
            self._reset_all(state)
            return ReconcileResult(
                "not_published", message="El contenedor de Instagram no es válido: no se publicó."
            )
        return ReconcileResult("unknown", message="No se pudo determinar el estado en Instagram.")

    def _record_recovered(self, ctx: PublishContext, check: ReconcileResult) -> None:
        state = ctx.state
        state["media_id"] = check.external_id or ""
        if check.external_url:
            state["permalink"] = check.external_url
            state["permalink_checked"] = True
        state["stage"] = "published"
        state.pop("commit_rejected", None)
        ctx.save_state(state)

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        state = ctx.state
        result = state.get("result")
        if result:
            return ReconcileResult(
                "published",
                external_id=result.get("external_id"),
                external_url=result.get("external_url"),
                message="Instagram confirma la publicación.",
            )
        if "media_id" in state:
            return ReconcileResult(
                "published",
                external_id=state.get("media_id") or None,
                external_url=state.get("permalink"),
                message="Instagram confirma la publicación.",
            )
        if state.get("commit_rejected"):
            return ReconcileResult(
                "not_published", message="Instagram rechazó la publicación: no se publicó nada."
            )
        if not state.get("commit_at"):
            return ReconcileResult("unknown", message="No hay constancia del envío a Instagram.")
        try:
            check = self._remote_check(ctx)
        except ProviderError as exc:
            return ReconcileResult("unknown", message=f"No se pudo comprobar en Instagram: {exc.message}")
        if check.outcome == "published":
            self._record_recovered(ctx, check)
            try:  # the engine stops here: finish the extras (permalink, first comment, URLs) best effort
                done = self._after_publish(ctx)
                check.external_id = done.external_id or check.external_id
                check.external_url = done.external_url or check.external_url
            except Exception:
                log.warning("Instagram post-publish extras failed during reconcile", exc_info=True)
        elif check.outcome == "not_published":
            mc.reopen_commit(ctx)
        return check

    # ---------------------------------------------------------------------------------- credentials

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        """Instagram via Facebook Login uses the Page token, which does not expire (§2.3)."""
        return None

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        mc.graph_get(http, settings, account.external_id, account.access_token, fields="id,username")

    # ------------------------------------------------------------------------------------ analytics

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> AccountSnapshot:
        mc.ensure_background_budget()
        ig = account.external_id
        metrics: dict[str, float | int] = {}
        info = mc.graph_get(
            http, settings, ig, account.access_token, fields="followers_count,follows_count,media_count"
        )
        for field_name, key in (
            ("followers_count", "followers"),
            ("follows_count", "following"),
            ("media_count", "posts_count"),
        ):
            if info.get(field_name) is not None:
                metrics[key] = int(info[field_name])
        since, until = mc.day_window(day)
        auth = mc.auth_params(settings, account.access_token)
        url = mc.graph_url(settings, f"{ig}/insights")
        window = {"period": "day", "since": str(since), "until": str(until)}
        try:
            series = mc.fetch_insights(http, url, {**auth, **window}, ["reach"])
            totals = mc.fetch_insights(
                http, url, {**auth, **window, "metric_type": "total_value"}, ACCOUNT_TOTAL_METRICS
            )
        except ProviderError as exc:
            if mc.is_token_error(exc) or exc.kind != ErrorKind.AUTH:
                raise
            log.info("Instagram account insights unavailable: %s", exc.code)
            series, totals = {}, {}
        if "reach" in series:
            value = mc.insight_value(series["reach"], day)
            if value is not None:
                metrics["reach"] = value
        for name, key in (("views", "views"), ("total_interactions", "interactions")):
            if name in totals:
                value = mc.insight_value(totals[name], day)
                if value is not None:
                    metrics[key] = value
        return AccountSnapshot(day=day, metrics=metrics)

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[PostMetricData]:
        mc.ensure_background_budget()
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        ig = account.external_id
        auth = mc.auth_params(settings, account.access_token)
        params = {
            **auth,
            "fields": "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,"
            "like_count,comments_count",
            "limit": "50",
        }
        posts: list[PostMetricData] = []
        insights_ok = True
        for item in mc.iter_paginated(http, mc.graph_url(settings, f"{ig}/media"), params, max_pages=5):
            published = mc.parse_time(item.get("timestamp"))
            if published is not None and published < since:
                break  # newest first
            media_id = str(item.get("id"))
            product = str(item.get("media_product_type") or "").upper()
            media_type = str(item.get("media_type") or "").upper()
            metrics: dict[str, float | int] = {}
            if item.get("like_count") is not None:
                metrics["likes"] = int(item["like_count"])
            if item.get("comments_count") is not None:
                metrics["comments"] = int(item["comments_count"])
            if insights_ok and not mc.background_paused():
                wanted = STORY_METRICS if product == "STORY" else MEDIA_METRICS
                try:
                    entries = mc.fetch_insights(
                        http, mc.graph_url(settings, f"{media_id}/insights"), dict(auth), wanted
                    )
                    for name, key in MEDIA_METRIC_KEYS.items():
                        if name in entries:
                            value = mc.insight_value(entries[name])
                            if value is not None:
                                metrics[key] = value
                except ProviderError as exc:
                    if mc.is_token_error(exc):
                        raise
                    if exc.kind == ErrorKind.RATE_LIMITED:
                        insights_ok = False
                    log.info("Instagram media insights unavailable: %s", exc.code)
            if "interactions" not in metrics and any(
                k in metrics for k in ("likes", "comments", "shares", "saves")
            ):
                metrics["interactions"] = sum(
                    int(metrics.get(k) or 0) for k in ("likes", "comments", "shares", "saves")
                )
            eng = mc.engagement(metrics)
            if eng is not None:
                metrics["engagement"] = eng
            if product == "REELS":
                kind = "REEL"
            elif product == "STORY":
                kind = "STORY"
            else:
                kind = media_type or None
            posts.append(
                PostMetricData(
                    external_id=media_id,
                    published_at=published,
                    permalink=item.get("permalink"),
                    caption=item.get("caption"),
                    media_type=kind,
                    thumbnail_url=item.get("thumbnail_url")
                    or (item.get("media_url") if media_type == "IMAGE" else None),
                    metrics=metrics,
                )
            )
        return posts

    # ------------------------------------------------------------------------------------- comments

    def _media_comments(
        self, http: httpx.Client, settings: ProviderSettings, auth: dict, media_id: str
    ) -> list[dict]:
        """Comments with replies; degrade the field set when ``from``/``replies`` are refused (§6.2)."""
        url = mc.graph_url(settings, f"{media_id}/comments")
        field_sets = [
            "id,text,timestamp,username,from{id,username},parent_id,"
            "replies{id,text,timestamp,username,from{id,username},parent_id}",
            "id,text,timestamp,username,parent_id,replies{id,text,timestamp,username,parent_id}",
            "id,text,timestamp,username",
        ]
        last: ProviderError | None = None
        for fields in field_sets:
            try:
                return list(
                    mc.iter_paginated(http, url, {**auth, "fields": fields, "limit": "50"}, max_pages=3)
                )
            except ProviderError as exc:
                if mc.is_token_error(exc) or exc.kind in (ErrorKind.RATE_LIMITED, ErrorKind.TRANSIENT):
                    raise
                last = exc
        if last is not None:
            log.info("Instagram comments unavailable for a media: %s", last.code)
        return []

    def fetch_comments(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[CommentData]:
        mc.ensure_background_budget()
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        ig = account.external_id
        own = self._own_usernames(account)
        auth = mc.auth_params(settings, account.access_token)
        posts_since = min(since, datetime.now(UTC) - timedelta(days=30))
        params = {**auth, "fields": "id,timestamp,permalink,comments_count", "limit": "25"}
        out: list[CommentData] = []
        seen: set[str] = set()

        def add(comment: dict, media_id: str, permalink: str | None, parent: str | None) -> None:
            comment_id = str(comment.get("id") or "")
            created = mc.parse_time(comment.get("timestamp"))
            if not comment_id or comment_id in seen or created is None or created < since:
                return
            seen.add(comment_id)
            author = comment.get("from") or {}
            username = comment.get("username") or author.get("username")
            author_id = str(author["id"]) if author.get("id") else None
            out.append(
                CommentData(
                    external_id=comment_id,
                    parent_external_id=parent
                    or (str(comment["parent_id"]) if comment.get("parent_id") else None),
                    object_external_id=media_id,
                    object_permalink=permalink,
                    author_name=username,
                    author_id=author_id,
                    text=str(comment.get("text") or ""),
                    created_time=created,
                    from_us=author_id == ig or (bool(username) and str(username).lower() in own),
                    raw={k: v for k, v in comment.items() if k != "replies"},
                )
            )

        for media in mc.iter_paginated(http, mc.graph_url(settings, f"{ig}/media"), params, max_pages=2):
            stamp = mc.parse_time(media.get("timestamp"))
            if stamp is not None and stamp < posts_since:
                break
            if not media.get("comments_count"):
                continue
            media_id = str(media.get("id"))
            permalink = media.get("permalink")
            for comment in self._media_comments(http, settings, auth, media_id):
                add(comment, media_id, permalink, None)
                for reply in mc.iter_edge(http, comment.get("replies"), auth, max_pages=2):
                    add(reply, media_id, permalink, str(comment.get("id")))
        return out

    def reply_comment(
        self,
        account: AccountCreds,
        settings: ProviderSettings,
        http: httpx.Client,
        comment_external_id: str,
        text: str,
    ) -> str:
        """Reply on ``/{comment}/replies`` (one level: an answer to a reply goes on its parent)."""
        if not (text or "").strip():
            raise ProviderError(ErrorKind.PERMANENT, "empty_reply", "La respuesta no puede estar vacía.")
        target = comment_external_id
        try:
            info = mc.graph_get(
                http, settings, comment_external_id, account.access_token, fields="id,parent_id"
            )
            if info.get("parent_id"):
                target = str(info["parent_id"])
        except ProviderError as exc:
            if exc.kind in (ErrorKind.AUTH, ErrorKind.RATE_LIMITED):
                raise
            log.info("Could not resolve the parent comment: %s", exc.code)
        body = mc.graph_post(http, settings, f"{target}/replies", account.access_token, {"message": text})
        reply_id = body.get("id")
        if not reply_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "ig_no_id", "Instagram no devolvió el identificador de la respuesta."
            )
        return str(reply_id)

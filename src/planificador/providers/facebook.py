"""Facebook Page provider (SPEC §5, §7, §10; research ``docs/research/02-meta-api.md`` §3, §5.1–§5.2, §6.1).

Publishing always uploads local bytes (no public URL is ever needed for Facebook, §0):

- ``post`` without media → ``POST /{page}/feed`` (``message`` + optional ``link``).
- ``post`` with 1 photo → ``POST /{page}/photos`` multipart ``source`` + ``published=true`` (the commit).
- ``post`` with 2–10 photos (or any photo post scheduled natively) → each photo staged with
  ``published=false`` (+ ``temporary=true`` when native), then ``POST /{page}/feed`` with ``attached_media``.
- ``post`` with 1 video → ``/{page}/videos`` chunked upload (``upload_phase`` start → transfer… → finish; the
  finish is the commit), falling back to a single multipart ``source`` upload if the chunked flow is refused.
- ``reel`` → ``/{page}/video_reels`` start → rupload bytes (``Authorization: OAuth``) → wait for the upload →
  finish with ``video_state=PUBLISHED`` (commit) → poll the publishing phase.
- ``story`` → photo staged unpublished + ``/{page}/photo_stories``, or ``/{page}/video_stories`` start →
  rupload → finish.
- ``native_schedule`` (post only, 15 min – 29 days ahead, like ``rules.FB_NATIVE_WINDOW``) →
  ``published=false`` + ``scheduled_publish_time``; the step returns ``published`` immediately with
  ``state.native_scheduled = True`` (SPEC §10). The lead is checked again right before the commit: when
  uploads or retries left less than :data:`NATIVE_COMMIT_MIN`, native scheduling is dropped and the post
  goes out directly at its time.

Every stage is persisted in ``ctx.state`` (re-entrant), ``ctx.before_commit`` runs right before the
irreversible request (``meta_common.run_commit``) and :meth:`FacebookProvider.reconcile` checks the remote
side (feed / scheduled posts / video status / stories) instead of re-sending.
"""

from __future__ import annotations

import logging
import math
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

#: §3.2 decision: re-encode to JPEG and keep files ≤ 4 MB.
PHOTO_MAX_BYTES = 4_000_000
MAX_PHOTOS = 10
#: §3.7: Meta allows 10 min – 30 days. We hand a post over from 15 min ahead (SPEC §7,
#: rules.FB_NATIVE_WINDOW, the option help) up to 29 days ahead, and re-check right before the commit that
#: ``scheduled_publish_time`` is still at least Meta's 10 min plus a margin (uploads/retries eat the lead).
NATIVE_MIN = timedelta(minutes=15)
NATIVE_MAX = timedelta(days=29)
NATIVE_COMMIT_MIN = timedelta(minutes=12)
EARLY_START_TOLERANCE_S = 5  # a non-native publish never starts more than this before its scheduled time
UPLOAD_POLL_S = 5
PUBLISH_POLL_S = 10
UPLOAD_WAIT_LIMIT = timedelta(minutes=15)
PUBLISH_WAIT_LIMIT = timedelta(minutes=20)
MAX_UPLOAD_FAILURES = 2
MAX_TRANSFER_FAILURES = 3

PAGE_METRICS = ["page_media_view", "page_total_media_view_unique", "page_post_engagements"]
PAGE_METRIC_KEYS = {
    "page_media_view": "views",
    "page_total_media_view_unique": "reach",
    "page_post_engagements": "interactions",
}
POST_METRICS = ["post_media_view", "post_total_media_view_unique"]

NATIVE_MESSAGE = "Programado en Facebook: se publicará a la hora indicada aunque el ordenador esté apagado."

_STATUS_TYPES = {
    "added_photos": "IMAGE",
    "added_video": "VIDEO",
    "shared_story": "LINK",
    "mobile_status_update": "TEXT",
    "created_event": "EVENT",
}


def _now_iso(ctx: PublishContext) -> str:
    return mc.iso(ctx.clock.now())


def _elapsed(ctx: PublishContext, started: str | None) -> timedelta:
    start = mc.parse_time(started)
    return ctx.clock.now() - start if start else timedelta(0)


def _video_failed(status: dict) -> str | None:
    """Error text when any phase of a Facebook video failed (§3.3 step 3)."""
    for phase in ("uploading_phase", "processing_phase", "publishing_phase"):
        info = status.get(phase) or {}
        error = info.get("error") or {}
        if info.get("status") == "error" or (isinstance(error, dict) and error.get("message")):
            return str((error or {}).get("message") or "error")
    if status.get("video_status") in ("error", "expired"):
        return str(status.get("video_status"))
    return None


def _upload_complete(status: dict) -> bool:
    uploading = (status.get("uploading_phase") or {}).get("status")
    return uploading == "complete" or status.get("video_status") in ("upload_complete", "ready", "processing")


class FacebookProvider(Provider):
    network = "facebook"
    display_name = "Facebook"
    formats = [
        FormatSpec("post", "Publicación", "images_or_video"),
        FormatSpec("reel", "Reel", "video"),
        FormatSpec("story", "Historia", "image_or_video"),
    ]
    supports_comments = True
    supports_analytics = True

    option_specs = [
        OptionSpec(
            "title",
            "Título",
            "text",
            None,
            help="Título del vídeo o reel (opcional).",
            formats=["post", "reel"],
        ),
        OptionSpec(
            "link",
            "Enlace",
            "text",
            None,
            help="Solo para publicaciones sin fotos ni vídeo.",
            formats=["post"],
        ),
        OptionSpec(
            "native_schedule",
            "Programar en Facebook",
            "bool",
            False,
            help="Facebook la publicará aunque el ordenador esté apagado (de 15 minutos a 29 días vista).",
            formats=["post"],
        ),
    ]

    # ------------------------------------------------------------------------------------- helpers

    @staticmethod
    def _page(ctx: PublishContext) -> str:
        return ctx.account.external_id

    @staticmethod
    def _post(
        ctx: PublishContext,
        path: str,
        data: dict[str, Any],
        *,
        files: dict | None = None,
        timeout: float | httpx.Timeout = 60.0,
    ) -> dict:
        return mc.graph_post(
            ctx.http, ctx.settings, path, ctx.account.access_token, data, files=files, timeout=timeout
        )

    @staticmethod
    def _get(ctx: PublishContext, path: str, **params: Any) -> dict:
        return mc.graph_get(ctx.http, ctx.settings, path, ctx.account.access_token, **params)

    @staticmethod
    def _images(ctx: PublishContext) -> list[MediaInfo]:
        return [m for m in ctx.target.media if m.kind == "image"]

    @staticmethod
    def _videos(ctx: PublishContext) -> list[MediaInfo]:
        return [m for m in ctx.target.media if m.kind == "video"]

    @staticmethod
    def _title(ctx: PublishContext) -> str | None:
        title = str(ctx.target.options.get("title") or "").strip()
        return title or None

    @staticmethod
    def _native_fields(state: dict) -> dict[str, Any]:
        if state.get("native"):
            return {"published": False, "scheduled_publish_time": state.get("native_time")}
        return {}

    def _check_native_lead(self, ctx: PublishContext) -> bool:
        """Drop native scheduling when ``scheduled_publish_time`` is now too close for Meta (§3.7: at least
        10 min after the request; slow uploads and engine retries eat the lead fixed at planning time).
        Returns ``True`` when it was dropped by this call; the post then goes out directly at its time."""
        state = ctx.state
        if not state.get("native"):
            return False
        try:
            native_time = int(state.get("native_time") or 0)
        except (TypeError, ValueError):
            native_time = 0
        lead = native_time - mc.to_unix(ctx.clock.now())
        if lead >= NATIVE_COMMIT_MIN.total_seconds():
            return False
        state["native"] = False
        state["native_time"] = None
        state["native_dropped"] = True
        ctx.save_state(state)
        ctx.log(
            "fb_native_dropped",
            "Queda muy poco para la hora programada y Facebook ya no acepta programarla: se publicará desde "
            "la app a la hora indicada.",
            {"lead_seconds": lead},
        )
        return True

    def _hold_until_due(self, ctx: PublishContext) -> StepResult | None:
        """After native scheduling was dropped, never commit before the scheduled time."""
        scheduled = ctx.target.scheduled_at
        if not ctx.state.get("native_dropped") or scheduled is None:
            return None
        delta = (scheduled - ctx.clock.now()).total_seconds()
        if delta <= EARLY_START_TOLERANCE_S:
            return None
        return StepResult(
            "wait",
            wait_seconds=max(1, math.ceil(delta)),
            message="Esperando a la hora programada para publicar en Facebook.",
        )

    def _native_gate(self, ctx: PublishContext) -> StepResult | None:
        """Right before a commit that may carry ``scheduled_publish_time``."""
        self._check_native_lead(ctx)
        return self._hold_until_due(ctx)

    @staticmethod
    def _post_id(page_id: str, post_id: Any) -> str | None:
        if not post_id:
            return None
        post_id = str(post_id)
        return post_id if "_" in post_id else f"{page_id}_{post_id}"

    # ------------------------------------------------------------------------------------ planning

    def _native_decision(self, ctx: PublishContext) -> tuple[str, int | None]:
        """("native", unix) | ("wait", seconds) | ("now", None)."""
        target = ctx.target
        if target.scheduled_at is None:
            return "now", None
        delta = target.scheduled_at - ctx.clock.now()
        if not target.options.get("native_schedule") or target.format != "post":
            # Not something Facebook can schedule: never publish before the scheduled time (the engine may
            # have claimed it early because the option was left on for another format).
            if delta.total_seconds() > EARLY_START_TOLERANCE_S:
                return "wait", max(1, math.ceil(delta.total_seconds()))
            return "now", None
        if delta > NATIVE_MAX:
            return "wait", int((delta - NATIVE_MAX).total_seconds()) + 60
        if delta >= NATIVE_MIN:
            return "native", mc.to_unix(target.scheduled_at)
        if delta.total_seconds() > 0:
            return "wait", max(1, math.ceil(delta.total_seconds()))
        ctx.log(
            "fb_native_skipped",
            "La hora programada ya ha llegado: se publica directamente en lugar de programarlo en Facebook.",
            None,
        )
        return "now", None

    def _plan(self, ctx: PublishContext) -> StepResult | None:
        target = ctx.target
        state = ctx.state
        images, videos = self._images(ctx), self._videos(ctx)
        if len(images) + len(videos) != len(target.media):
            raise ProviderError(ErrorKind.PERMANENT, "fb_media_kind", "Facebook solo admite fotos y vídeos.")
        fmt = target.format
        decision, value = self._native_decision(ctx)
        if decision == "wait":
            return StepResult(
                "wait",
                wait_seconds=int(value or 60),
                message="Esperando al momento de enviar la publicación a Facebook.",
            )
        native = decision == "native"  # only ever for ``post``
        native_time = value if native else None
        if fmt == "post":
            if videos and (images or len(videos) > 1):
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "fb_mixed_media",
                    "En Facebook una publicación lleva hasta 10 fotos o un solo vídeo, pero no ambos.",
                )
            if len(images) > MAX_PHOTOS:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "fb_too_many_photos",
                    f"Facebook admite como máximo {MAX_PHOTOS} fotos por publicación.",
                )
            if videos:
                flow = "video"
            elif images:
                flow = "photos" if (len(images) > 1 or native) else "photo_single"
            else:
                if not (target.text or "").strip() and not str(target.options.get("link") or "").strip():
                    raise ProviderError(
                        ErrorKind.PERMANENT,
                        "fb_empty",
                        "La publicación de Facebook no tiene texto, enlace ni archivos.",
                    )
                flow = "text"
        elif fmt == "reel":
            if len(videos) != 1 or images:
                raise ProviderError(
                    ErrorKind.PERMANENT, "fb_reel_media", "Un reel de Facebook necesita exactamente un vídeo."
                )
            flow = "reel"
        elif fmt == "story":
            if len(target.media) != 1:
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "fb_story_media",
                    "Una historia de Facebook lleva una sola foto o un solo vídeo.",
                )
            flow = "story_photo" if images else "story_video"
        else:
            raise ProviderError(ErrorKind.PERMANENT, "fb_format", "Formato de Facebook no disponible.")
        state.update({"v": 1, "flow": flow, "stage": "start", "native": native, "native_time": native_time})
        ctx.save_state(state)
        return None

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
        if not state.get("flow"):
            early = self._plan(ctx)
            if early is not None:
                return early
        if state.get("stage") == "committing":
            # A previous commit was sent without a recorded outcome: look before sending again.
            check = self._remote_check(ctx)
            if check.outcome == "published":
                return self._finish(ctx, check.external_id, check.external_url, check.message)
            if check.outcome == "unknown":
                raise ProviderError(
                    ErrorKind.AMBIGUOUS,
                    "fb_commit_unknown",
                    "No se pudo confirmar si Facebook publicó el contenido. Revísalo en Facebook antes de "
                    "reintentar.",
                )
            mc.reopen_commit(ctx)
        flow = state["flow"]
        handler = {
            "text": self._flow_text,
            "photo_single": self._flow_photo_single,
            "photos": self._flow_photos,
            "video": self._flow_video,
            "reel": self._flow_reel,
            "story_photo": self._flow_story_photo,
            "story_video": self._flow_story_video,
        }.get(flow)
        if handler is None:
            raise ProviderError(
                ErrorKind.PERMANENT, "fb_bad_state", "Estado de publicación de Facebook no válido."
            )
        return handler(ctx)

    def _committed(self, ctx: PublishContext, **values: Any) -> None:
        state = ctx.state
        state.update({k: v for k, v in values.items() if v is not None})
        state["committed"] = True
        state.pop("commit_rejected", None)
        state["stage"] = "committed"
        ctx.save_state(state)

    def _finish(
        self, ctx: PublishContext, external_id: str | None, url: str | None, message: str | None = None
    ) -> StepResult:
        state = ctx.state
        if state.get("native"):
            state["native_scheduled"] = True
            message = message or NATIVE_MESSAGE
        message = message or "Publicado en Facebook."
        state["committed"] = True
        state["stage"] = "done"
        state["result"] = {"external_id": external_id, "external_url": url, "message": message}
        ctx.save_state(state)
        return StepResult("published", external_id=external_id, external_url=url, message=message)

    def _permalink(self, ctx: PublishContext, object_id: str) -> str | None:
        """Best effort: never fail a live post over metadata (§3.1/§3.3)."""
        try:
            body = self._get(ctx, object_id, fields="permalink_url")
        except ProviderError as exc:
            log.info("Facebook permalink lookup failed: %s", exc.code)
            return None
        return mc.absolute_facebook_url(body.get("permalink_url"))

    def _finish_post(self, ctx: PublishContext, post_id: str) -> StepResult:
        return self._finish(ctx, post_id, self._permalink(ctx, post_id))

    # -- text / link -------------------------------------------------------------------------------

    def _flow_text(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        held = self._native_gate(ctx)
        if held is not None:
            return held
        state["stage"] = "commit"
        text = ctx.target.text or ""
        link = str(ctx.target.options.get("link") or "").strip() or None
        if state.get("link_dropped") and link:
            if link not in text:
                text = f"{text}\n\n{link}".strip()
            link = None
        data = {"message": text or None, "link": link, **self._native_fields(state)}
        page = self._page(ctx)
        try:
            body = mc.run_commit(
                ctx, "Facebook", lambda: self._post(ctx, f"{page}/feed", data), sent={"message": text}
            )
        except ProviderError as exc:
            if exc.code == "fb_link_scrape" and link and not state.get("link_dropped"):
                state["link_dropped"] = True
                ctx.save_state(state)
                ctx.log(
                    "fb_link_dropped",
                    "Facebook no pudo leer el enlace: se publica con el enlace en el texto, sin vista "
                    "previa.",
                    None,
                )
                return StepResult("continue")
            raise
        post_id = str(body.get("id") or "")
        if not post_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS, "fb_no_id", "Facebook no devolvió el identificador de la publicación."
            )
        self._committed(ctx, post_id=post_id)
        return self._finish_post(ctx, post_id)

    # -- photos ------------------------------------------------------------------------------------

    def _upload_photo(
        self, ctx: PublishContext, info: MediaInfo, *, published: bool, temporary: bool = False
    ) -> str:
        path, mime = mc.prepare_image(info, PHOTO_MAX_BYTES, accept=("image/jpeg", "image/png"))
        data = {"published": published, "temporary": True if temporary else None}
        name = path.name if path != info.path else (info.original_name or path.name)
        with path.open("rb") as fh:
            body = self._post(
                ctx,
                f"{self._page(ctx)}/photos",
                data,
                files={"source": (name, fh, mime)},
                timeout=mc.UPLOAD_TIMEOUT,
            )
        photo_id = body.get("id")
        if not photo_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "fb_no_id", "Facebook no devolvió el identificador de la foto."
            )
        return str(photo_id)

    def _discard_photos(self, ctx: PublishContext) -> None:
        """Delete staged unpublished photos after a definite failure (§3.2 step 3); best effort."""
        state = ctx.state
        for photo_id in state.get("photo_ids") or []:
            if not photo_id:
                continue
            try:
                mc.graph_delete(ctx.http, ctx.settings, str(photo_id), ctx.account.access_token)
            except ProviderError as exc:
                log.info("Could not delete staged photo: %s", exc.code)
        state["photo_ids"] = []
        state["stage"] = "start"
        ctx.save_state(state)

    def _flow_photo_single(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        held = self._hold_until_due(ctx)
        if held is not None:
            return held
        info = self._images(ctx)[0]
        path, mime = mc.prepare_image(info, PHOTO_MAX_BYTES, accept=("image/jpeg", "image/png"))
        name = path.name if path != info.path else (info.original_name or path.name)
        text = ctx.target.text or ""
        state["stage"] = "commit"

        def call() -> dict:
            with path.open("rb") as fh:
                return self._post(
                    ctx,
                    f"{self._page(ctx)}/photos",
                    {"message": text or None, "published": True},
                    files={"source": (name, fh, mime)},
                    timeout=mc.UPLOAD_TIMEOUT,
                )

        body = mc.run_commit(ctx, "Facebook", call, sent={"message": text})
        post_id = self._post_id(self._page(ctx), body.get("post_id")) or (
            str(body["id"]) if body.get("id") else None
        )
        if not post_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS, "fb_no_id", "Facebook no devolvió el identificador de la foto."
            )
        self._committed(ctx, post_id=post_id, photo_id=str(body.get("id") or ""))
        return self._finish_post(ctx, post_id)

    def _flow_photos(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        images = self._images(ctx)
        if self._check_native_lead(ctx):
            # Photos staged for a scheduled post are ``temporary``: stage them again for a direct post.
            if any(state.get("photo_ids") or []):
                self._discard_photos(ctx)
            if len(images) == 1:
                state["flow"] = "photo_single"  # a single photo only went through /feed to be scheduled
                state["stage"] = "start"
                ctx.save_state(state)
                return StepResult("continue")
        ids: list[str | None] = list(state.get("photo_ids") or [])
        if len(ids) != len(images):
            ids = [None] * len(images)
        for index, info in enumerate(images):
            if ids[index]:
                continue
            ids[index] = self._upload_photo(ctx, info, published=False, temporary=bool(state.get("native")))
            state["photo_ids"] = ids
            state["stage"] = "staging"
            ctx.save_state(state)
            ctx.log(
                "fb_photo_uploaded",
                f"Foto {index + 1} de {len(images)} subida a Facebook.",
                {"photo_id": ids[index]},
            )
            return StepResult("continue", message="Subiendo fotos a Facebook…")
        held = self._hold_until_due(ctx)
        if held is not None:
            return held
        state["stage"] = "commit"
        text = ctx.target.text or ""
        data: dict[str, Any] = {"message": text or None}
        for index, photo_id in enumerate(ids):
            data[f"attached_media[{index}]"] = {"media_fbid": photo_id}
        data.update(self._native_fields(state))
        page = self._page(ctx)
        try:
            body = mc.run_commit(
                ctx, "Facebook", lambda: self._post(ctx, f"{page}/feed", data), sent={"message": text}
            )
        except ProviderError as exc:
            if exc.kind == ErrorKind.PERMANENT:
                self._discard_photos(ctx)
            raise
        post_id = str(body.get("id") or "")
        if not post_id:
            raise ProviderError(
                ErrorKind.AMBIGUOUS, "fb_no_id", "Facebook no devolvió el identificador de la publicación."
            )
        self._committed(ctx, post_id=post_id)
        return self._finish_post(ctx, post_id)

    # -- video (/videos chunked upload) -----------------------------------------------------------

    def _reset_video(self, ctx: PublishContext) -> None:
        state = ctx.state
        for key in ("video_id", "upload_session_id", "start_offset", "end_offset", "upload_url", "uploaded"):
            state.pop(key, None)
        state["stage"] = "start"
        state["transfer_failures"] = 0
        ctx.save_state(state)

    def _flow_video(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        info = self._videos(ctx)[0]
        page = self._page(ctx)
        stage = state.get("stage") or "start"
        if stage in ("finish", "simple_commit"):
            held = self._native_gate(ctx)
            if held is not None:
                return held

        if stage == "start":
            if state.get("video_mode") == "simple":
                state["stage"] = "simple_commit"
                ctx.save_state(state)
                return StepResult("continue")
            size = mc.media_size(info.path, info.original_name)
            try:
                body = self._post(ctx, f"{page}/videos", {"upload_phase": "start", "file_size": size})
            except ProviderError as exc:
                if exc.kind == ErrorKind.PERMANENT and exc.detail.get("code") == 100:
                    state["video_mode"] = "simple"
                    state["stage"] = "simple_commit"
                    ctx.save_state(state)
                    ctx.log(
                        "fb_video_simple",
                        "Facebook no aceptó la subida por partes: el vídeo se subirá de una vez.",
                        exc.detail,
                    )
                    return StepResult("continue")
                raise
            try:
                state.update(
                    {
                        "video_id": str(body["video_id"]),
                        "upload_session_id": str(body["upload_session_id"]),
                        "start_offset": int(body["start_offset"]),
                        "end_offset": int(body["end_offset"]),
                        "stage": "transfer",
                        "transfer_failures": 0,
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError(
                    ErrorKind.TRANSIENT,
                    "fb_bad_response",
                    "Facebook devolvió una respuesta inesperada al subir el vídeo.",
                ) from exc
            ctx.save_state(state)
            ctx.log(
                "fb_video_upload_started", "Subiendo el vídeo a Facebook…", {"video_id": state["video_id"]}
            )
            return StepResult("continue", message="Subiendo el vídeo a Facebook…")

        if stage == "transfer":
            start, end = int(state["start_offset"]), int(state["end_offset"])
            if start < end:
                with info.path.open("rb") as fh:
                    fh.seek(start)
                    chunk = fh.read(end - start)
                try:
                    body = self._post(
                        ctx,
                        f"{page}/videos",
                        {
                            "upload_phase": "transfer",
                            "upload_session_id": state["upload_session_id"],
                            "start_offset": start,
                        },
                        files={"video_file_chunk": ("chunk.bin", chunk, "application/octet-stream")},
                        timeout=mc.UPLOAD_TIMEOUT,
                    )
                    state["start_offset"] = int(body["start_offset"])
                    state["end_offset"] = int(body["end_offset"])
                except (KeyError, TypeError, ValueError, ProviderError) as exc:
                    failures = int(state.get("transfer_failures") or 0) + 1
                    state["transfer_failures"] = failures
                    ctx.save_state(state)
                    if failures >= MAX_TRANSFER_FAILURES:
                        self._reset_video(ctx)
                    if isinstance(exc, ProviderError):
                        raise
                    raise ProviderError(
                        ErrorKind.TRANSIENT,
                        "fb_bad_response",
                        "Facebook devolvió una respuesta inesperada al subir el vídeo.",
                    ) from exc
                state["transfer_failures"] = 0
                ctx.save_state(state)
                return StepResult("continue", message="Subiendo el vídeo a Facebook…")
            state["stage"] = "finish"
            ctx.save_state(state)
            stage = "finish"
            held = self._native_gate(ctx)
            if held is not None:
                return held

        if stage == "finish":
            text = ctx.target.text or ""
            data = {
                "upload_phase": "finish",
                "upload_session_id": state["upload_session_id"],
                "description": text or None,
                "title": self._title(ctx),
                **self._native_fields(state),
            }
            body = mc.run_commit(
                ctx, "Facebook", lambda: self._post(ctx, f"{page}/videos", data), sent={"message": text}
            )
            if body.get("success") is False:
                mc.mark_commit_rejected(ctx)
                raise ProviderError(
                    ErrorKind.PERMANENT, "fb_video_finish_failed", "Facebook no aceptó el vídeo."
                )
            return self._after_video_commit(ctx, state["video_id"])

        if stage == "simple_commit":
            text = ctx.target.text or ""
            data = {"description": text or None, "title": self._title(ctx), **self._native_fields(state)}

            def call() -> dict:
                with info.path.open("rb") as fh:
                    return self._post(
                        ctx,
                        f"{page}/videos",
                        data,
                        files={
                            "source": (info.original_name or info.path.name, fh, info.mime or "video/mp4")
                        },
                        timeout=mc.UPLOAD_TIMEOUT,
                    )

            body = mc.run_commit(ctx, "Facebook", call, sent={"message": text})
            video_id = str(body.get("id") or "")
            if not video_id:
                raise ProviderError(
                    ErrorKind.AMBIGUOUS, "fb_no_id", "Facebook no devolvió el identificador del vídeo."
                )
            return self._after_video_commit(ctx, video_id)

        if stage in ("processing", "publishing", "committed"):
            return self._poll_publishing(ctx, state["video_id"])
        raise ProviderError(
            ErrorKind.PERMANENT, "fb_bad_state", "Estado de publicación de Facebook no válido."
        )

    def _after_video_commit(
        self, ctx: PublishContext, video_id: str, post_id: str | None = None
    ) -> StepResult:
        state = ctx.state
        self._committed(ctx, video_id=video_id, post_id=self._post_id(self._page(ctx), post_id))
        if state.get("native"):
            return self._finish(ctx, video_id, None)
        state["stage"] = "publishing"
        state["poll_started_at"] = _now_iso(ctx)
        ctx.save_state(state)
        ctx.log("fb_video_processing", "Facebook está procesando el vídeo.", {"video_id": video_id})
        return StepResult("wait", wait_seconds=PUBLISH_POLL_S, message="Facebook está procesando el vídeo…")

    def _video_status(self, ctx: PublishContext, video_id: str) -> dict:
        return self._get(ctx, video_id, fields="status,post_id,permalink_url,published")

    def _poll_publishing(self, ctx: PublishContext, video_id: str) -> StepResult:
        state = ctx.state
        body = self._video_status(ctx, video_id)
        status = body.get("status") or {}
        page = self._page(ctx)
        post_id = self._post_id(page, body.get("post_id")) or state.get("post_id")
        url = mc.absolute_facebook_url(body.get("permalink_url"))
        failure = _video_failed(status)
        if failure:
            # Facebook will not publish this video: record a definite "not published" and start over on retry.
            state["committed"] = False
            state["commit_rejected"] = True
            state.pop("post_id", None)
            self._reset_video(ctx)
            raise ProviderError(
                ErrorKind.PERMANENT,
                "fb_video_processing_failed",
                "Facebook no pudo procesar el vídeo. Revisa el formato (MP4 H.264 + AAC recomendado).",
                detail={"video_status": status.get("video_status"), "error": failure},
            )
        publishing = (status.get("publishing_phase") or {}).get("status")
        video_status = status.get("video_status")
        done = (
            publishing == "complete"
            or video_status == "published"
            or (video_status == "ready" and publishing in (None, "complete"))
            or (body.get("published") is True and publishing in (None, "complete"))
        )
        if done:
            return self._finish(ctx, post_id or video_id, url)
        if _elapsed(ctx, state.get("poll_started_at")) > PUBLISH_WAIT_LIMIT:
            return self._finish(
                ctx,
                post_id or video_id,
                url,
                "Facebook ha aceptado el vídeo y sigue procesándolo; aparecerá en la página en unos minutos.",
            )
        return StepResult("wait", wait_seconds=PUBLISH_POLL_S, message="Facebook está procesando el vídeo…")

    # -- reels and video stories (rupload) --------------------------------------------------------

    def _flow_reel(self, ctx: PublishContext) -> StepResult:
        return self._rupload_flow(ctx, "video_reels")

    def _flow_story_video(self, ctx: PublishContext) -> StepResult:
        return self._rupload_flow(ctx, "video_stories")

    def _rupload_flow(self, ctx: PublishContext, edge: str) -> StepResult:
        state = ctx.state
        info = self._videos(ctx)[0]
        page = self._page(ctx)
        stage = state.get("stage") or "start"
        is_story = edge == "video_stories"

        if stage == "start":
            body = self._post(ctx, f"{page}/{edge}", {"upload_phase": "start"})
            video_id = body.get("video_id")
            if not video_id:
                raise ProviderError(
                    ErrorKind.TRANSIENT, "fb_no_id", "Facebook no devolvió el identificador del vídeo."
                )
            upload_url = body.get("upload_url") or (
                f"https://{mc.RUPLOAD_HOST}/video-upload/{mc.api_version(ctx.settings)}/{video_id}"
            )
            state.update(
                {
                    "video_id": str(video_id),
                    "upload_url": str(upload_url),
                    "stage": "upload",
                    "uploaded": False,
                }
            )
            ctx.save_state(state)
            return StepResult("continue", message="Subiendo el vídeo a Facebook…")

        if stage == "upload":
            try:
                mc.rupload(
                    ctx.http,
                    state["upload_url"],
                    ctx.account.access_token,
                    info.path,
                    prefixes=("/video-upload/",),
                )
            except ProviderError as exc:
                failures = int(state.get("upload_failures") or 0) + 1
                state["upload_failures"] = failures
                ctx.save_state(state)
                if failures >= MAX_UPLOAD_FAILURES or exc.kind == ErrorKind.PERMANENT:
                    state["upload_failures"] = 0
                    self._reset_video(ctx)
                raise
            state.update(
                {
                    "uploaded": True,
                    "stage": "upload_wait",
                    "poll_started_at": _now_iso(ctx),
                    "upload_failures": 0,
                }
            )
            ctx.save_state(state)
            ctx.log("fb_video_uploaded", "Vídeo subido a Facebook.", {"video_id": state["video_id"]})
            return StepResult("continue")

        if stage == "upload_wait":
            body = self._get(ctx, state["video_id"], fields="status")
            status = body.get("status") or {}
            failure = _video_failed(status)
            if failure:
                self._reset_video(ctx)
                raise ProviderError(
                    ErrorKind.PERMANENT,
                    "fb_video_upload_failed",
                    "Facebook no pudo procesar el vídeo subido. Revisa el formato (MP4 H.264 + AAC, 9:16).",
                    detail={"error": failure},
                )
            if _upload_complete(status):
                state["stage"] = "finish"
                ctx.save_state(state)
                stage = "finish"
            elif _elapsed(ctx, state.get("poll_started_at")) > UPLOAD_WAIT_LIMIT:
                self._reset_video(ctx)
                raise ProviderError(
                    ErrorKind.TRANSIENT,
                    "fb_upload_timeout",
                    "Facebook tardó demasiado en recibir el vídeo. Se reintentará.",
                )
            else:
                return StepResult(
                    "wait", wait_seconds=UPLOAD_POLL_S, message="Facebook está recibiendo el vídeo…"
                )

        if stage == "finish":
            text = ctx.target.text or ""
            data: dict[str, Any] = {"upload_phase": "finish", "video_id": state["video_id"]}
            if not is_story:
                data.update(
                    {"video_state": "PUBLISHED", "description": text or None, "title": self._title(ctx)}
                )
            body = mc.run_commit(
                ctx, "Facebook", lambda: self._post(ctx, f"{page}/{edge}", data), sent={"message": text}
            )
            if body.get("success") is False:
                mc.mark_commit_rejected(ctx)
                raise ProviderError(
                    ErrorKind.PERMANENT, "fb_video_finish_failed", "Facebook no aceptó el vídeo."
                )
            post_id = self._post_id(page, body.get("post_id"))
            if is_story:
                self._committed(ctx, post_id=post_id)
                return self._finish(
                    ctx, post_id or state["video_id"], None, "Historia publicada en Facebook."
                )
            return self._after_video_commit(ctx, state["video_id"], body.get("post_id"))

        if stage in ("publishing", "committed"):
            return self._poll_publishing(ctx, state["video_id"])
        raise ProviderError(
            ErrorKind.PERMANENT, "fb_bad_state", "Estado de publicación de Facebook no válido."
        )

    # -- photo story -------------------------------------------------------------------------------

    def _flow_story_photo(self, ctx: PublishContext) -> StepResult:
        state = ctx.state
        ids = [i for i in (state.get("photo_ids") or []) if i]
        if not ids:
            photo_id = self._upload_photo(ctx, self._images(ctx)[0], published=False)
            state["photo_ids"] = [photo_id]
            state["stage"] = "commit"
            ctx.save_state(state)
            ctx.log("fb_photo_uploaded", "Foto de la historia subida a Facebook.", {"photo_id": photo_id})
            return StepResult("continue")
        page = self._page(ctx)
        state["stage"] = "commit"
        body = mc.run_commit(
            ctx, "Facebook", lambda: self._post(ctx, f"{page}/photo_stories", {"photo_id": ids[0]}), sent={}
        )
        if body.get("success") is False:
            mc.mark_commit_rejected(ctx)
            raise ProviderError(ErrorKind.PERMANENT, "fb_story_failed", "Facebook no aceptó la historia.")
        post_id = self._post_id(page, body.get("post_id"))
        self._committed(ctx, post_id=post_id)
        return self._finish(ctx, post_id or ids[0], None, "Historia publicada en Facebook.")

    # ------------------------------------------------------------------------------------ reconcile

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        state = ctx.state
        result = state.get("result")
        if result:
            return ReconcileResult(
                "published",
                external_id=result.get("external_id"),
                external_url=result.get("external_url"),
                message="Facebook confirma la publicación.",
            )
        if state.get("committed"):
            external_id = state.get("post_id") or state.get("video_id")
            return ReconcileResult(
                "published", external_id=external_id, message="Facebook confirma la publicación."
            )
        if state.get("commit_rejected"):
            return ReconcileResult("not_published", message="Facebook rechazó el envío: no se publicó nada.")
        if not state.get("commit_at"):
            return ReconcileResult(
                "unknown",
                message="No hay constancia del envío a Facebook. Revisa la página antes de reintentar.",
            )
        try:
            check = self._remote_check(ctx)
        except ProviderError as exc:
            return ReconcileResult("unknown", message=f"No se pudo comprobar en Facebook: {exc.message}")
        if check.outcome == "published":
            if state.get("native"):
                state["native_scheduled"] = True
            state["committed"] = True
            state["stage"] = "done"
            state["result"] = {
                "external_id": check.external_id,
                "external_url": check.external_url,
                "message": check.message,
            }
            ctx.save_state(state)
        elif check.outcome == "not_published":
            mc.reopen_commit(ctx)
        return check

    def _remote_check(self, ctx: PublishContext) -> ReconcileResult:
        state = ctx.state
        flow = state.get("flow")
        if flow in ("text", "photos", "photo_single"):
            return self._find_feed_post(ctx)
        if flow == "story_photo":
            ids = [i for i in (state.get("photo_ids") or []) if i]
            if not ids:
                return ReconcileResult("not_published", message="La foto de la historia no llegó a subirse.")
            return self._find_story(ctx, ids[0])
        if flow == "story_video":
            video_id = state.get("video_id")
            if not video_id:
                return ReconcileResult("not_published", message="El vídeo de la historia no llegó a subirse.")
            found = self._find_story(ctx, video_id)
            if found.outcome == "published":
                return found
            return self._check_video(ctx, video_id)
        if flow in ("reel", "video"):
            if state.get("video_id"):
                return self._check_video(ctx, state["video_id"])
            return self._find_video_by_description(ctx)
        return ReconcileResult("unknown", message="No se pudo determinar el estado en Facebook.")

    def _sent_message(self, ctx: PublishContext) -> str:
        sent = ctx.state.get("sent") or {}
        return mc.normalize_text(sent.get("message") if "message" in sent else ctx.target.text)

    def _commit_time(self, ctx: PublishContext) -> datetime:
        return mc.parse_time(ctx.state.get("commit_at")) or ctx.clock.now()

    def _find_feed_post(self, ctx: PublishContext) -> ReconcileResult:
        state = ctx.state
        text = self._sent_message(ctx)
        if not text:
            return ReconcileResult(
                "unknown",
                message="La publicación no tenía texto y no se puede localizar automáticamente en Facebook.",
            )
        page = self._page(ctx)
        auth = mc.auth_params(ctx.settings, ctx.account.access_token)
        if state.get("native"):
            url = mc.graph_url(ctx.settings, f"{page}/scheduled_posts")
            params = {**auth, "fields": "id,message,scheduled_publish_time,created_time", "limit": "50"}
        else:
            since = self._commit_time(ctx) - timedelta(minutes=10)
            url = mc.graph_url(ctx.settings, f"{page}/feed")
            params = {
                **auth,
                "fields": "id,message,created_time,permalink_url",
                "since": str(mc.to_unix(since)),
                "limit": "25",
            }
        for item in mc.iter_paginated(ctx.http, url, params, max_pages=3):
            if mc.normalize_text(item.get("message")) == text:
                return ReconcileResult(
                    "published",
                    external_id=str(item.get("id")),
                    external_url=mc.absolute_facebook_url(item.get("permalink_url")),
                    message=NATIVE_MESSAGE if state.get("native") else "Facebook muestra la publicación.",
                )
        return ReconcileResult(
            "not_published", message="Facebook no muestra la publicación: no llegó a publicarse."
        )

    def _find_story(self, ctx: PublishContext, media_id: str) -> ReconcileResult:
        page = self._page(ctx)
        params = {
            **mc.auth_params(ctx.settings, ctx.account.access_token),
            "fields": "post_id,media_id,status,creation_time,url",
            "limit": "25",
        }
        items = list(
            mc.iter_paginated(ctx.http, mc.graph_url(ctx.settings, f"{page}/stories"), params, max_pages=2)
        )
        for item in items:
            if str(item.get("media_id") or "") == str(media_id):
                return ReconcileResult(
                    "published",
                    external_id=self._post_id(page, item.get("post_id")) or str(media_id),
                    external_url=item.get("url"),
                    message="Facebook muestra la historia.",
                )
        if all("media_id" in item for item in items):
            return ReconcileResult(
                "not_published", message="Facebook no muestra la historia: no llegó a publicarse."
            )
        return ReconcileResult("unknown", message="No se pudo comprobar la historia en Facebook.")

    def _check_video(self, ctx: PublishContext, video_id: str) -> ReconcileResult:
        state = ctx.state
        body = self._get(
            ctx, video_id, fields="status,post_id,permalink_url,published,scheduled_publish_time"
        )
        status = body.get("status") or {}
        publishing = status.get("publishing_phase") or {}
        page = self._page(ctx)
        external_id = self._post_id(page, body.get("post_id")) or video_id
        url = mc.absolute_facebook_url(body.get("permalink_url"))
        if state.get("native") and (
            body.get("scheduled_publish_time") or publishing.get("publish_status") == "scheduled"
        ):
            return ReconcileResult("published", external_id=video_id, message=NATIVE_MESSAGE)
        if (
            publishing.get("status") in ("complete", "in_progress")
            or status.get("video_status") == "published"
            or body.get("published") is True
        ):
            return ReconcileResult(
                "published", external_id=external_id, external_url=url, message="Facebook confirma el vídeo."
            )
        if _video_failed(status):
            return ReconcileResult(
                "not_published", message="Facebook no pudo procesar el vídeo: no se publicó."
            )
        if publishing.get("status") in (None, "not_started"):
            return ReconcileResult("not_published", message="Facebook no ha publicado el vídeo.")
        return ReconcileResult("unknown", message="No se pudo determinar el estado del vídeo en Facebook.")

    def _find_video_by_description(self, ctx: PublishContext) -> ReconcileResult:
        text = self._sent_message(ctx)
        if not text:
            return ReconcileResult(
                "unknown", message="El vídeo no tenía descripción y no se puede localizar en Facebook."
            )
        since = self._commit_time(ctx) - timedelta(minutes=10)
        params = {
            **mc.auth_params(ctx.settings, ctx.account.access_token),
            "fields": "id,description,created_time,permalink_url",
            "limit": "10",
        }
        url = mc.graph_url(ctx.settings, f"{self._page(ctx)}/videos")
        for item in mc.iter_paginated(ctx.http, url, params, max_pages=1):
            created = mc.parse_time(item.get("created_time"))
            if created and created < since:
                continue
            if mc.normalize_text(item.get("description")) == text:
                return ReconcileResult(
                    "published",
                    external_id=str(item.get("id")),
                    external_url=mc.absolute_facebook_url(item.get("permalink_url")),
                    message="Facebook muestra el vídeo.",
                )
        return ReconcileResult(
            "not_published", message="Facebook no muestra el vídeo: no llegó a publicarse."
        )

    # ------------------------------------------------------------------------- native schedule cancel

    def delete_post(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, external_id: str
    ) -> bool:
        """Delete a post (e.g. cancel a natively scheduled one, SPEC §10). False on failure.

        A post the user already deleted on Facebook answers ``100/33`` ("object does not exist"): that counts
        as withdrawn, but only when the Page itself is still readable with the same token — otherwise the
        error may mean lost access, and the post could still go live."""
        try:
            body = mc.graph_delete(http, settings, external_id, account.access_token)
        except ProviderError as exc:
            if exc.code == "meta_not_found" and self._page_readable(account, settings, http):
                log.info("Facebook delete: the post no longer exists; treated as withdrawn")
                return True
            log.info("Facebook delete failed: %s", exc.code)
            return False
        return body.get("success") is not False

    def _page_readable(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> bool:
        try:
            body = mc.graph_get(http, settings, account.external_id, account.access_token, fields="id")
        except ProviderError:
            return False
        return str(body.get("id") or "") == str(account.external_id)

    def cancel_native(self, ctx: PublishContext) -> None:
        """Withdraw a post handed to Facebook's own scheduler (engine ``cancel_target``)."""
        result = ctx.state.get("result") if isinstance(ctx.state.get("result"), dict) else {}
        external_id = (result or {}).get("external_id")
        if not external_id:
            raise ProviderError(
                ErrorKind.PERMANENT,
                "fb_native_no_id",
                "No consta el identificador de la publicación programada en Facebook.",
            )
        if not self.delete_post(ctx.account, ctx.settings, ctx.http, str(external_id)):
            raise ProviderError(
                ErrorKind.PERMANENT, "fb_native_delete_failed", "Facebook no permitió retirarla."
            )
        ctx.state["native_scheduled"] = False
        ctx.state["native_cancelled"] = True
        ctx.save_state(ctx.state)

    # ---------------------------------------------------------------------------------- credentials

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        """Page tokens derived from a long-lived user token do not expire (§2.3): nothing to refresh. A broken
        token surfaces as ``AUTH`` from :meth:`check_account` or any call."""
        return None

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        mc.graph_get(http, settings, account.external_id, account.access_token, fields="id,name")

    # ------------------------------------------------------------------------------------ analytics

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> AccountSnapshot:
        mc.ensure_background_budget()
        page = account.external_id
        metrics: dict[str, float | int] = {}
        info = mc.graph_get(http, settings, page, account.access_token, fields="followers_count,fan_count")
        followers = info.get("followers_count")
        if followers is None:
            followers = info.get("fan_count")
        if followers is not None:
            metrics["followers"] = int(followers)
        since, until = mc.series_window(day)
        params = {
            **mc.auth_params(settings, account.access_token),
            "period": "day",
            "since": str(since),
            "until": str(until),
        }
        try:
            entries = mc.fetch_insights(
                http, mc.graph_url(settings, f"{page}/insights"), params, PAGE_METRICS
            )
        except ProviderError as exc:
            if mc.is_token_error(exc) or exc.kind != ErrorKind.AUTH:
                raise
            log.info("Facebook page insights unavailable: %s", exc.code)  # e.g. read_insights not granted
            entries = {}
        for name, key in PAGE_METRIC_KEYS.items():
            if name in entries:
                value = mc.insight_value(entries[name], day)
                if value is not None:
                    metrics[key] = value
        return AccountSnapshot(day=day, metrics=metrics)

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[PostMetricData]:
        mc.ensure_background_budget()
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        page = account.external_id
        auth = mc.auth_params(settings, account.access_token)
        params = {
            **auth,
            "fields": "id,message,created_time,permalink_url,full_picture,status_type,shares,"
            "reactions.limit(0).summary(true),comments.limit(0).summary(true)",
            "since": str(mc.to_unix(since)),
            "limit": "50",
        }
        posts: list[PostMetricData] = []
        insights_ok = True
        for item in mc.iter_paginated(http, mc.graph_url(settings, f"{page}/posts"), params, max_pages=5):
            created = mc.parse_time(item.get("created_time"))
            if created is not None and created < since:
                continue
            post_id = str(item.get("id"))
            likes = ((item.get("reactions") or {}).get("summary") or {}).get("total_count")
            comments = ((item.get("comments") or {}).get("summary") or {}).get("total_count")
            shares = (item.get("shares") or {}).get("count", 0)
            metrics: dict[str, float | int] = {
                "likes": int(likes or 0),
                "comments": int(comments or 0),
                "shares": int(shares or 0),
            }
            if insights_ok and not mc.background_paused():
                try:
                    entries = mc.fetch_insights(
                        http, mc.graph_url(settings, f"{post_id}/insights"), dict(auth), POST_METRICS
                    )
                    for name, key in (
                        ("post_media_view", "views"),
                        ("post_total_media_view_unique", "reach"),
                    ):
                        if name in entries:
                            value = mc.insight_value(entries[name])
                            if value is not None:
                                metrics[key] = value
                except ProviderError as exc:
                    if mc.is_token_error(exc):
                        raise
                    if exc.kind in (ErrorKind.RATE_LIMITED, ErrorKind.AUTH):
                        insights_ok = False
                    log.info("Facebook post insights unavailable for a post: %s", exc.code)
            metrics["interactions"] = metrics["likes"] + metrics["comments"] + metrics["shares"]
            eng = mc.engagement(metrics)
            if eng is not None:
                metrics["engagement"] = eng
            posts.append(
                PostMetricData(
                    external_id=post_id,
                    published_at=created,
                    permalink=mc.absolute_facebook_url(item.get("permalink_url")),
                    caption=item.get("message"),
                    media_type=_STATUS_TYPES.get(str(item.get("status_type") or ""), "TEXT"),
                    thumbnail_url=item.get("full_picture"),
                    metrics=metrics,
                )
            )
        return posts

    # ------------------------------------------------------------------------------------- comments

    def fetch_comments(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[CommentData]:
        """Comments (and replies, via ``filter(stream)``) on the Page's posts of the last 30 days (§6.1)."""
        mc.ensure_background_budget()
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        page = account.external_id
        auth = mc.auth_params(settings, account.access_token)
        posts_since = min(since, datetime.now(UTC) - timedelta(days=30))
        comment_fields = "id,message,created_time,from{id,name},parent{id},permalink_url"
        params = {
            **auth,
            "fields": "id,permalink_url,created_time,"
            f"comments.limit(50).filter(stream).order(reverse_chronological){{{comment_fields}}}",
            "since": str(mc.to_unix(posts_since)),
            "limit": "25",
        }
        out: list[CommentData] = []
        seen: set[str] = set()
        for post in mc.iter_paginated(http, mc.graph_url(settings, f"{page}/feed"), params, max_pages=4):
            post_id = str(post.get("id"))
            for comment in mc.iter_edge(http, post.get("comments"), auth, max_pages=3):
                comment_id = str(comment.get("id") or "")
                created = mc.parse_time(comment.get("created_time"))
                if not comment_id or comment_id in seen or created is None or created < since:
                    continue
                seen.add(comment_id)
                author = comment.get("from") or {}
                parent = comment.get("parent") or {}
                out.append(
                    CommentData(
                        external_id=comment_id,
                        parent_external_id=str(parent["id"]) if parent.get("id") else None,
                        object_external_id=post_id,
                        object_permalink=mc.absolute_facebook_url(post.get("permalink_url")),
                        author_name=author.get("name"),
                        author_id=str(author["id"]) if author.get("id") else None,
                        text=str(comment.get("message") or ""),
                        created_time=created,
                        from_us=str(author.get("id") or "") == page,
                        raw=comment,
                    )
                )
        return out

    def reply_comment(
        self,
        account: AccountCreds,
        settings: ProviderSettings,
        http: httpx.Client,
        comment_external_id: str,
        text: str,
    ) -> str:
        """Reply on ``/{comment}/comments`` (two levels: an answer to a reply goes on its parent)."""
        if not (text or "").strip():
            raise ProviderError(ErrorKind.PERMANENT, "empty_reply", "La respuesta no puede estar vacía.")
        target = comment_external_id
        try:
            info = mc.graph_get(
                http, settings, comment_external_id, account.access_token, fields="id,parent{id}"
            )
            parent_id = str((info.get("parent") or {}).get("id") or "")
            post_part = comment_external_id.split("_", 1)[0] if "_" in comment_external_id else ""
            if parent_id and parent_id != post_part:
                target = parent_id
        except ProviderError as exc:
            if exc.kind in (ErrorKind.AUTH, ErrorKind.RATE_LIMITED):
                raise
            log.info("Could not resolve the parent comment: %s", exc.code)
        body = mc.graph_post(http, settings, f"{target}/comments", account.access_token, {"message": text})
        reply_id = body.get("id")
        if not reply_id:
            raise ProviderError(
                ErrorKind.TRANSIENT, "fb_no_id", "Facebook no devolvió el identificador de la respuesta."
            )
        return str(reply_id)

"""Demo provider: a fake network with **no network calls** (SPEC §5).

Used when ``modo prueba`` (demo mode) is on, and by tests to drive the publishing engine.

Behaviour is controlled by ``target.options``:

- ``demo_steps`` (int, default 2): total ``publish_step`` calls for a successful publish. Every call except
  the last returns ``wait`` (1 s); the last one commits. 2 → one ``wait`` of 1 s, then ``published``.
- ``demo_fail``: ``transient`` | ``rate_limited`` | ``auth`` | ``permanent`` | ``ambiguous`` | ``none``.
  ``transient``/``rate_limited``/``auth``/``permanent`` raise **before** the commit point (``before_commit``
  is not called). ``ambiguous`` raises **after** ``ctx.before_commit`` (the outcome is unknown).
- ``demo_fail_times`` (int): how many times to fail before succeeding. Absent → fail every time.
- ``demo_result`` (extension): ``published`` (default) | ``sent_to_app`` (simulates TikTok inbox mode).
- ``demo_reconcile`` (extension): what :meth:`DemoProvider.reconcile` reports when the remote outcome was not
  recorded (after an ``ambiguous`` failure): ``unknown`` (default) | ``published`` | ``not_published``.

On success ``external_id = "demo-<target_id>"`` and ``external_url = None``.

Analytics and comments return deterministic, clearly labelled ("[Demo]") data derived from the account id —
only when ``settings.demo_mode`` is on (empty otherwise).
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, time, timedelta

import httpx

from planificador.providers.base import (
    AccountCreds,
    AccountSnapshot,
    CommentData,
    ErrorKind,
    FormatSpec,
    Issue,
    OptionSpec,
    PostMetricData,
    Provider,
    ProviderError,
    ProviderSettings,
    PublishContext,
    ReconcileResult,
    StepResult,
    TargetSpec,
    TokenUpdate,
)
from planificador.timeutil import Clock, format_date_es

DEMO_LABEL = "[Demo]"
WAIT_SECONDS = 1
TOKEN_REFRESH_MARGIN = timedelta(hours=1)
DEMO_TOKEN_LIFETIME = timedelta(days=60)
_EPOCH = date(2026, 1, 1)

FAIL_KINDS: dict[str, ErrorKind] = {
    "transient": ErrorKind.TRANSIENT,
    "rate_limited": ErrorKind.RATE_LIMITED,
    "auth": ErrorKind.AUTH,
    "permanent": ErrorKind.PERMANENT,
    "ambiguous": ErrorKind.AMBIGUOUS,
}
FAIL_MESSAGES: dict[str, str] = {
    "transient": "Fallo temporal simulado en la red Demo. Se reintentará.",
    "rate_limited": "Límite de peticiones simulado en la red Demo. Se reintentará más tarde.",
    "auth": "La cuenta Demo necesita reconexión (simulado).",
    "permanent": "La red Demo ha rechazado la publicación (simulado).",
    "ambiguous": "La red Demo no respondió tras enviar la publicación (simulado).",
}

_SAMPLE_COMMENTS = (
    "¡Me encanta esta publicación!",
    "¿Cuándo sale la próxima?",
    "Muy útil, gracias por compartir.",
    "¿Dónde puedo comprarlo?",
    "Qué buena foto 😍",
    "Totalmente de acuerdo.",
    "¿Tenéis envíos a Canarias?",
    "Interesante, lo probaré.",
)
_MEDIA_TYPES = ("IMAGE", "VIDEO", "CAROUSEL_ALBUM")


def _rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))


def _int_option(options: dict, key: str, default: int | None) -> int | None:
    value = options.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DemoProvider(Provider):
    network = "demo"
    display_name = "Demo"
    formats = [FormatSpec("post", "Publicación", "images_or_video")]
    supports_comments = True
    supports_analytics = True

    option_specs = [
        OptionSpec(
            "demo_steps", "Pasos simulados", "number", 2, help="Llamadas hasta publicar (1 = inmediato)."
        ),
        OptionSpec(
            "demo_fail",
            "Simular fallo",
            "select",
            "none",
            choices=[
                ("none", "Sin fallo"),
                ("transient", "Temporal"),
                ("rate_limited", "Límite de peticiones"),
                ("auth", "Requiere reconexión"),
                ("permanent", "Rechazo permanente"),
                ("ambiguous", "Resultado desconocido"),
            ],
        ),
        OptionSpec("demo_fail_times", "Veces que falla", "number", None, help="Vacío = falla siempre."),
    ]

    # ------------------------------------------------------------------------------------ validation

    def validate(self, target: TargetSpec) -> list[Issue]:
        issues: list[Issue] = []
        if not (target.text or "").strip() and not target.media:
            issues.append(
                Issue("error", "demo_empty", "Escribe un texto o añade al menos un archivo.", "text")
            )
        if target.format not in {f.key for f in self.formats}:
            issues.append(Issue("error", "demo_format", "Formato no disponible en Demo.", "format"))
        return issues

    # ------------------------------------------------------------------------------------ publishing

    def _failure(self, ctx: PublishContext) -> str | None:
        """The configured failure kind if it should fire on this attempt (counts it in the state)."""
        fail = str(ctx.target.options.get("demo_fail") or "none").strip().lower()
        if fail not in FAIL_KINDS:
            return None
        times = _int_option(ctx.target.options, "demo_fail_times", None)
        failures = int(ctx.state.get("failures", 0))
        if times is not None and failures >= times:
            return None
        return fail

    def _raise(self, ctx: PublishContext, fail: str) -> None:
        ctx.state["failures"] = int(ctx.state.get("failures", 0)) + 1
        ctx.save_state(ctx.state)
        raise ProviderError(
            FAIL_KINDS[fail],
            f"demo_{fail}",
            FAIL_MESSAGES[fail],
            detail={"simulated": True, "attempt": ctx.state["failures"]},
            retry_after=60 if fail == "rate_limited" else None,
        )

    def publish_step(self, ctx: PublishContext) -> StepResult:
        steps = max(1, _int_option(ctx.target.options, "demo_steps", 2) or 1)
        done = int(ctx.state.get("steps_done", 0))

        if ctx.state.get("remote_published"):
            # Re-entered after a crash that happened after the remote side accepted the post.
            return self._success(ctx)

        if done < steps - 1:
            ctx.state["steps_done"] = done + 1
            ctx.save_state(ctx.state)
            ctx.log("demo_processing", f"Procesando en Demo (paso {done + 1} de {steps}).", None)
            return StepResult("wait", wait_seconds=WAIT_SECONDS, message="Procesando en Demo…")

        fail = self._failure(ctx)
        if fail is not None and fail != "ambiguous":
            self._raise(ctx, fail)

        ctx.state["steps_done"] = steps
        ctx.state["commit_attempts"] = int(ctx.state.get("commit_attempts", 0)) + 1
        ctx.before_commit(ctx.state)  # --- irreversible point ---

        if fail == "ambiguous":
            self._raise(ctx, fail)

        ctx.state["remote_published"] = True
        ctx.state["remote_id"] = f"demo-{ctx.target_id}"
        ctx.save_state(ctx.state)
        return self._success(ctx)

    def _success(self, ctx: PublishContext) -> StepResult:
        result = str(ctx.target.options.get("demo_result") or "published")
        if result == "sent_to_app":
            return StepResult(
                "sent_to_app",
                external_id=f"demo-{ctx.target_id}",
                message="Borrador enviado a la app Demo (simulado).",
            )
        return StepResult(
            "published",
            external_id=f"demo-{ctx.target_id}",
            external_url=None,
            message="Publicado en Demo (simulado).",
        )

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        if ctx.state.get("remote_published"):
            return ReconcileResult(
                "published",
                external_id=f"demo-{ctx.target_id}",
                message="La red Demo confirma la publicación.",
            )
        outcome = str(ctx.target.options.get("demo_reconcile") or "unknown").strip().lower()
        if outcome == "published":
            return ReconcileResult(
                "published",
                external_id=f"demo-{ctx.target_id}",
                message="La red Demo confirma la publicación.",
            )
        if outcome == "not_published":
            return ReconcileResult("not_published", message="La red Demo indica que no se publicó.")
        return ReconcileResult("unknown", message="No se pudo confirmar en Demo si la publicación salió.")

    # ---------------------------------------------------------------------------------- credentials

    def refresh_credentials(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, clock: Clock
    ) -> TokenUpdate | None:
        """Issue a new fake token when the current one expires within the hour."""
        now = clock.now()
        if account.token_expires_at is None or account.token_expires_at - now > TOKEN_REFRESH_MARGIN:
            return None
        return TokenUpdate(
            access_token=f"demo-access-{account.id}-{int(now.timestamp())}",
            token_expires_at=now + DEMO_TOKEN_LIFETIME,
        )

    def check_account(self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client) -> None:
        if account.meta.get("demo_broken"):
            raise ProviderError(ErrorKind.AUTH, "demo_auth", FAIL_MESSAGES["auth"])

    # ------------------------------------------------------------------------------------ analytics

    def fetch_account_snapshot(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, day: date
    ) -> AccountSnapshot:
        if not settings.demo_mode:
            return AccountSnapshot(day=day, metrics={})
        rng = _rng("snapshot", account.id, day.isoformat())
        days = (day - _EPOCH).days
        base = 800 + (account.id * 397) % 4200
        growth = 3 + account.id % 5
        followers = max(0, base + days * growth + rng.randint(-6, 6))
        weekend = 1.3 if day.weekday() >= 5 else 1.0
        views = int(rng.randint(400, 2500) * weekend)
        reach = int(views * rng.uniform(0.55, 0.85))
        interactions = int(reach * rng.uniform(0.03, 0.09))
        return AccountSnapshot(
            day=day,
            metrics={
                "followers": followers,
                "following": 120 + account.id % 80,
                "posts_count": 20 + max(0, days) // 3,
                "views": views,
                "reach": reach,
                "interactions": interactions,
                "profile_views": rng.randint(10, 120),
            },
        )

    def _demo_posts(self, account: AccountCreds, since: datetime) -> list[tuple[str, datetime, int]]:
        """Deterministic demo posts in ``[since, since + 30 days]``: (external_id, published_at, index)."""
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        start = since.astimezone(UTC).date()
        out: list[tuple[str, datetime, int]] = []
        for offset in range(31):
            d = start + timedelta(days=offset)
            ordinal = d.toordinal()
            if (ordinal + account.id) % 3:
                continue
            at = datetime.combine(d, time(9 + (account.id + ordinal) % 10, (ordinal * 7) % 60), tzinfo=UTC)
            if at < since or at > since + timedelta(days=30):
                continue
            out.append((f"demo-{account.id}-{d.isoformat()}", at, ordinal))
        return out

    def fetch_recent_posts(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[PostMetricData]:
        if not settings.demo_mode:
            return []
        posts: list[PostMetricData] = []
        for external_id, published_at, ordinal in self._demo_posts(account, since):
            rng = _rng("post", account.id, external_id)
            views = rng.randint(300, 5000)
            reach = int(views * rng.uniform(0.6, 0.9))
            likes = int(reach * rng.uniform(0.02, 0.08))
            comments = rng.randint(0, 40)
            shares = rng.randint(0, 25)
            saves = rng.randint(0, 30)
            interactions = likes + comments + shares + saves
            engagement = round(interactions / reach * 100, 2) if reach else 0.0
            posts.append(
                PostMetricData(
                    external_id=external_id,
                    published_at=published_at,
                    permalink=None,
                    caption=f"{DEMO_LABEL} Publicación de ejemplo del {format_date_es(published_at.date())}",
                    media_type=_MEDIA_TYPES[ordinal % len(_MEDIA_TYPES)],
                    thumbnail_url=None,
                    metrics={
                        "views": views,
                        "reach": reach,
                        "likes": likes,
                        "comments": comments,
                        "shares": shares,
                        "saves": saves,
                        "interactions": interactions,
                        "engagement": engagement,
                    },
                )
            )
        return posts

    # ------------------------------------------------------------------------------------- comments

    def fetch_comments(
        self, account: AccountCreds, settings: ProviderSettings, http: httpx.Client, since: datetime
    ) -> list[CommentData]:
        if not settings.demo_mode:
            return []
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        out: list[CommentData] = []
        for external_id, published_at, _ordinal in self._demo_posts(account, since):
            rng = _rng("comments", account.id, external_id)
            for i in range(rng.randint(0, 3)):
                created = published_at + timedelta(minutes=37 * (i + 1))
                if created < since:
                    continue
                author_n = rng.randint(1, 40)
                out.append(
                    CommentData(
                        external_id=f"demo-c-{external_id}-{i}",
                        parent_external_id=None,
                        object_external_id=external_id,
                        object_permalink=None,
                        author_name=f"Usuario demo {author_n}",
                        author_id=f"demo-user-{author_n}",
                        text=f"{DEMO_LABEL} {_SAMPLE_COMMENTS[rng.randrange(len(_SAMPLE_COMMENTS))]}",
                        created_time=created,
                        from_us=False,
                        raw={"demo": True},
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
        if not text.strip():
            raise ProviderError(ErrorKind.PERMANENT, "demo_empty_reply", "La respuesta no puede estar vacía.")
        digest = hashlib.sha256(f"{comment_external_id}|{text}".encode()).hexdigest()[:12]
        return f"demo-reply-{digest}"

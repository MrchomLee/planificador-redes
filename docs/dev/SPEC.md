# Planificador de Redes — Technical specification (v1)

This is the single source of truth for every agent building the app. The research it is based on lives in
`docs/research/01..04*.md` (read the relevant sections before implementing a module — they contain exact
endpoints, parameters, limits and error codes, each with a "Verificación" section at the end whose corrections
take precedence over the body).

Licensing rule: the AGPL reference projects (Postiz, Brightbean, OpenPost) may be READ to learn API behavior, but
no code is copied. Our code is original. Project license: MIT.

---

## 1. Product

A **local, single-user** web app that runs on the user's Windows PC (also Linux/macOS) at
`http://localhost:8765` and replicates the core of Metricool with our own name and look:

- **Marcas** (brands): containers of connected accounts (one account per network per brand).
- **Cuentas conectadas**: Facebook Page, Instagram professional account (via Facebook Login), TikTok.
  Plus a **Demo** network (fake provider) usable when `modo prueba` is on.
- **Planificación**: calendar (Mes / Semana / Día / Lista), drag & drop rescheduling, filters, search,
  "Mejores horas" shading, **Ideas** board (undated posts), **Papelera** (trash, 45 days).
- **Nueva publicación** (composer): network selection, "Común" tab + one tab per network with overrides,
  per-network formats/options, media from library or upload, live validation, live previews, date/time in the
  brand's timezone, auto-publish toggle per network, actions: Programar · Guardar borrador · Guardar como idea ·
  Publicar ahora.
- **Publicación asistida** (Metricool's manual/notification mode): at the scheduled time, a desktop + in-app
  notification; a modal with *Copiar texto*, *Descargar medios*, *Abrir red* and *Marcar como publicado*.
  For TikTok it can instead send the video as a **draft to the TikTok app inbox**.
- **Biblioteca de medios**: upload, dedupe, thumbnails, metadata, per-network compatibility badges.
- **Estadísticas** (analytics): daily snapshots, KPI cards with % change vs previous period, charts, post table,
  CSV export, best-times heatmap.
- **Comentarios** (inbox): Facebook + Instagram comments, reply, mark read (polling).
- **Importar CSV / Exportar CSV** of posts.
- **Ajustes**: Meta/TikTok developer-app credentials, Graph API version, public-media mode (Cloudflare quick
  tunnel / own public base URL / off), timezone, grace window for late posts, demo mode, optional password,
  notifications.
- Windows launchers (`iniciar.bat`, start-with-Windows shortcut) and a Spanish user guide (Meta app + TikTok app
  setup step by step).

Out of scope for v1: recurrence, queues/autolists, approvals/teams, reports PDF, competitors, hashtag tracker,
link-in-bio, ads, URL shortener, AI assistant, image/video editor (only basic checks + conversions).

UI language: **Spanish** everywhere (labels, errors, dates: "lunes 3 de noviembre, 10:30").
Code, identifiers, comments, commit messages: English.

---

## 2. Stack and layout

- Python **>= 3.12** (user has 3.14 on Windows; CI/dev container has 3.13), managed with **uv**.
- FastAPI + Uvicorn (sync `def` endpoints — run in the threadpool), Jinja2 templates, **htmx 2.0.11**,
  **Alpine.js 3.17.4**, **FullCalendar 6.1.21** (global bundle + `es` locale), **Chart.js 4.5.1**,
  **lucide-static** (icons, ISC) and **simple-icons** (network logos, CC0) — all **vendored** into
  `src/planificador/static/vendor/` by `scripts/vendor_assets.py` (downloads npm tarballs from
  registry.npmjs.org, verifies integrity, extracts only the needed files, writes `static/vendor/VERSIONS.txt`
  with versions + licenses). Vendored files are committed; the user never needs Node.
- SQLAlchemy 2.x ORM on SQLite (WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`).
- httpx (sync client) for all HTTP; **respx** + **time-machine** + pytest for tests; Playwright for smoke tests.
- Pillow (+ optional pillow-heif), **PyAV (`av`)** for video metadata and poster frames, `cryptography`
  (Fernet) + `keyring` for secrets, `tzdata`, `desktop-notifier` (optional; failures are non-fatal),
  `python-multipart`, `itsdangerous`.

```
planificador-redes/
  pyproject.toml  uv.lock  README.md  LICENSE  iniciar.bat  iniciar.sh
  scripts/vendor_assets.py  scripts/crear_acceso_inicio.ps1
  docs/research/*  docs/dev/SPEC.md  docs/guia-meta.md  docs/guia-tiktok.md
  src/planificador/
    __init__.py  __main__.py (CLI: `planificador` → run server + open browser)
    config.py      # Settings loaded from env + DB settings table; defaults
    paths.py       # data dir resolution
    db.py          # engine, session factory, pragmas, init/migrations
    models.py      # ALL ORM models (section 4)
    crypto.py      # secret encryption (keyring master key + Fernet; key-file fallback)
    security.py    # Host/Origin check, CSRF, optional password login
    timeutil.py    # Clock abstraction, tz helpers, Spanish date formatting
    templating.py  # Jinja env, filters, global context (active brand, nav, notifications count)
    app.py         # create_app(): middleware, routers, static, lifespan (scheduler + services)
    deps.py        # FastAPI dependencies (db session, clock, settings, active brand)
    rules.py       # per-network validation rules (data-driven) — section 7
    media.py       # media ingest/metadata/thumbnails/derivatives — section 8
    publicmedia.py # public media server + cloudflared quick tunnel manager — section 9
    engine.py      # publishing engine (state machine runner) — section 10
    scheduler.py   # background thread: due posts, retries, catch-up, periodic jobs — section 10
    notify.py      # notifications (DB + desktop) — section 11
    analytics.py   # snapshots, aggregation, best-times — section 12
    inbox.py       # comment polling + reply — section 13
    csvio.py       # CSV import/export — section 14
    providers/
      __init__.py  base.py  registry.py  demo.py
      meta_common.py  facebook.py  instagram.py  tiktok.py
    routes/
      __init__.py  planner.py  posts.py  composer.py  media.py  analytics.py  inbox.py
      connections.py  brands.py  settings.py  notifications.py  csv.py  pages.py
    templates/  (base.html, partials/, planner/, composer/, media/, analytics/, inbox/,
                 connections/, brands/, settings/, notifications/, csv/, errors/)
    static/css/app.css  static/js/{app.js, planner.js, composer.js, analytics.js, media.js}
    static/img/ static/vendor/
  tests/ (conftest.py + test_*.py per module; tests/e2e/ Playwright)
```

Entry point: `[project.scripts] planificador = "planificador.__main__:main"`. `main()` parses
`--port` (default 8765), `--no-browser`, `--data-dir`; enforces a single instance (lock file in data dir; a second
launch only opens the browser at the running instance); starts Uvicorn bound to **127.0.0.1**; opens
`http://localhost:<port>/` when ready.

**Always use the host name `localhost`** in generated URLs and OAuth redirect URIs (cookies set on `localhost`
are not sent to `127.0.0.1`).

Data dir: `PLANIFICADOR_DATA_DIR` env, else Windows `%LOCALAPPDATA%\PlanificadorRedes`, else
`~/.local/share/planificador-redes`. Inside: `planificador.sqlite3`, `media/originals/ab/<sha256>.<ext>`,
`media/thumbs/`, `media/derived/` (e.g. JPEG conversions), `bin/` (cloudflared), `logs/app.log`
(rotating), `secret.key` (only if keyring unavailable), `instance.lock`.

---

## 3. Security (local web app)

- Bind to 127.0.0.1 only. Middleware rejects requests whose `Host` header is not `localhost:<port>` or
  `127.0.0.1:<port>` (DNS-rebinding defense) with 400 — except the public media app (separate server, section 9).
- CSRF: every state-changing request (POST/PUT/PATCH/DELETE) must carry a valid CSRF token (header
  `X-CSRF-Token`, set globally for htmx in `app.js` via `htmx:configRequest`, or form field `csrf_token`) **and**
  an `Origin`/`Referer` matching our origin when present. GET routes never change state, except the OAuth
  callbacks (validated by `state`).
- Session cookie (Starlette SessionMiddleware, secret from crypto module): `HttpOnly`, `SameSite=Lax`
  (Strict would break OAuth redirects), path `/`.
- Optional password (Ajustes): if set, all routes except `/login`, `/static/*` and OAuth callbacks require a
  logged-in session. Hash with `hashlib.scrypt` (salted).
- Secrets (app secrets, access/refresh tokens): encrypted at rest with Fernet; master key stored via `keyring`
  (service `planificador-redes`, user `master-key`); fallback to `<data>/secret.key` (0600). Encrypted values are
  prefixed `v1:`. Never log tokens; `ProviderError.detail` must be scrubbed of tokens.
- Media served to the browser only from our own routes with ids (no path params mapping to filesystem paths).
- Jinja autoescape on. User text rendered with `|e`. JSON embedded in templates via `|tojson`.

---

## 4. Data model (`models.py`, SQLAlchemy 2 declarative, all timestamps UTC naive-in-DB via a TZ-aware
type decorator that stores UTC and returns aware UTC datetimes)

Enums are `StrEnum`s stored as strings.

```
Network: facebook | instagram | tiktok | demo
AccountStatus: connected | needs_reconnect | disconnected
TargetStatus: draft | scheduled | publishing | published | manual_pending | manual_done |
              sent_to_app | error | missed | unknown | cancelled
   - draft: saved, not scheduled (post has date or not)
   - scheduled: waiting for scheduled_at (auto_publish true or false)
   - publishing: engine working on it (claimed)
   - published: confirmed live (external_id/url when available)
   - manual_pending: Publicación asistida — time reached, user must publish by hand
   - manual_done: user marked it as published by hand
   - sent_to_app: TikTok draft delivered to the app inbox (user finishes there)
   - error: failed permanently or retries exhausted (last_error shown)
   - missed: was due while the app was off beyond the grace window
   - unknown: commit was sent but the outcome could not be confirmed — user must check (never auto-retried)
   - cancelled: removed from schedule
PostKind: scheduled | idea
IdeaStage: idea | en_progreso | lista       (Ideas board columns)
CommitPhase: none | prepared | commit_sent
```

Tables:

- `settings`: `key` (PK str), `value` (JSON), `updated_at`. Secret values stored encrypted inside JSON as
  `{"enc": "v1:..."}`. Helper API in `config.py`: `get_setting(db, key, default)`, `set_setting(db, key, value,
  secret=False)`, `get_secret(db, key)`.
- `brands`: `id`, `name`, `color` (hex), `timezone` (IANA, default from settings, default `Europe/Madrid`),
  `created_at`, `archived_at`.
- `accounts`: `id`, `brand_id` FK, `network`, `external_id` (page id / ig user id / tiktok open_id / demo id),
  `name`, `username`, `avatar_path` (local cached file, nullable), `account_type` (`page`, `business`,
  `creator`, `personal`, `demo`), `status`, `access_token_enc`, `refresh_token_enc`, `token_expires_at`,
  `refresh_expires_at`, `scopes` (JSON list), `meta` (JSON: e.g. IG `page_id`; TikTok `creator_info` cache;
  FB `tasks`), `connected_at`, `last_checked_at`, `last_error`. Unique (`brand_id`, `network`).
- `media_assets`: `id`, `brand_id` (nullable = shared), `kind` (`image`|`video`), `original_name`,
  `rel_path` (relative to media dir), `sha256` (unique), `size_bytes`, `mime`, `width`, `height`,
  `duration_s`, `fps`, `video_codec`, `audio_codec`, `bitrate`, `is_hdr`, `rotation`, `thumb_rel_path`,
  `created_at`, `deleted_at`.
- `posts`: `id`, `brand_id` FK, `kind` (PostKind), `idea_stage` (nullable), `text` (base/"Común" text),
  `scheduled_at` (nullable; UTC), `notes`, `source` (`composer`|`csv`|`duplicate`), `created_at`,
  `updated_at`, `deleted_at` (trash).
- `post_media`: `post_id` FK, `media_id` FK, `position` — base media list (ordered).
- `post_targets`: `id`, `post_id` FK, `account_id` FK, `network`, `format` (see section 7), `text_override`
  (nullable), `media_override` (JSON list of media ids, nullable), `options` (JSON), `auto_publish` (bool),
  `status` (TargetStatus), `scheduled_at` (UTC, mirrors post), `attempts` (int), `next_attempt_at`,
  `commit_phase` (CommitPhase), `provider_state` (JSON), `external_id`, `external_url`, `published_at`,
  `last_error` (Spanish message), `last_error_detail` (JSON, scrubbed), `claimed_at`, `created_at`,
  `updated_at`. Index (`status`, `scheduled_at`), (`status`, `next_attempt_at`).
- `post_events`: `id`, `target_id` FK, `ts`, `level` (`info`|`warning`|`error`), `event` (short code),
  `message` (Spanish), `data` (JSON) — audit trail shown in the post detail drawer.
- `notifications`: `id`, `created_at`, `kind` (`manual_publish`|`error`|`reconnect`|`unknown`|`missed`|
  `info`), `title`, `body`, `target_id` (nullable), `account_id` (nullable), `read_at`, `desktop_sent_at`.
- `analytics_snapshots`: `id`, `account_id` FK, `day` (date), `metrics` (JSON {name: number}), `fetched_at`.
  Unique (`account_id`, `day`).
- `post_metrics`: `id`, `account_id` FK, `external_id`, `target_id` (nullable FK), `published_at`,
  `permalink`, `caption`, `media_type`, `thumbnail_url`, `metrics` (JSON), `fetched_at`.
  Unique (`account_id`, `external_id`).
- `inbox_comments`: `id`, `account_id` FK, `external_id`, `parent_external_id` (nullable), `object_external_id`
  (post/media id), `object_permalink`, `author_name`, `author_id`, `text`, `created_time`, `from_us` (bool),
  `read_at`, `replied_at`, `raw` (JSON). Unique (`account_id`, `external_id`).
- `oauth_states`: `state` (PK), `network`, `brand_id`, `code_verifier` (nullable), `created_at`
  (expire after 10 min).
- `public_media_tokens`: `token` (PK, 128-bit urlsafe), `rel_path`, `mime`, `expires_at`, `created_at`,
  `hits`, `revoked_at`.
- `schema_version`: single row with integer version; `db.init_db()` creates tables (`create_all`) and runs
  ordered migration functions for future versions.

Derived "post status" for display (planner cards): priority error > unknown > missed > manual_pending >
publishing > scheduled > sent_to_app > draft > published/manual_done (all published ⇒ published).

---

## 5. Provider interface (`providers/base.py`) — CONTRACT shared by all agents

```python
class ErrorKind(StrEnum): TRANSIENT, RATE_LIMITED, AUTH, PERMANENT, AMBIGUOUS

class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, code: str, message: str, *, detail: dict | None = None,
                 retry_after: int | None = None): ...
    # message: Spanish, user-facing. detail: scrubbed technical info (http status, api error code, log_id...).

@dataclass
class MediaInfo:  # built from MediaAsset
    id: int; kind: str; path: Path; mime: str; size_bytes: int
    width: int | None; height: int | None; duration_s: float | None; fps: float | None
    video_codec: str | None; audio_codec: str | None; bitrate: int | None; is_hdr: bool
    original_name: str

@dataclass
class TargetSpec:
    network: str; format: str; text: str; media: list[MediaInfo]; options: dict
    scheduled_at: datetime | None; auto_publish: bool

@dataclass
class Issue:
    level: Literal["error", "warning"]; code: str; message: str; field: str | None = None

@dataclass
class AccountCreds:
    id: int; network: str; external_id: str; name: str; account_type: str | None
    access_token: str | None; refresh_token: str | None
    token_expires_at: datetime | None; refresh_expires_at: datetime | None
    meta: dict

@dataclass
class TokenUpdate:
    access_token: str; token_expires_at: datetime | None
    refresh_token: str | None = None; refresh_expires_at: datetime | None = None
    scopes: list[str] | None = None

@dataclass
class ProviderSettings:  # built by config.provider_settings(db)
    meta_app_id: str | None; meta_app_secret: str | None; meta_api_version: str  # default "v25.0"
    tiktok_client_key: str | None; tiktok_client_secret: str | None
    tiktok_pkce_encoding: str  # "hex" (default) | "base64url"
    base_url: str  # "http://localhost:8765"
    demo_mode: bool

class PublicMedia(Protocol):   # implemented by publicmedia.PublicMediaService
    def url_for(self, path: Path, mime: str, ttl_seconds: int = 3600) -> str: ...  # public HTTPS URL
    def release(self, url: str) -> None: ...

@dataclass
class PublishContext:
    account: AccountCreds
    target: TargetSpec
    target_id: int
    state: dict                       # provider_state (mutable copy)
    settings: ProviderSettings
    http: httpx.Client
    clock: Clock                      # timeutil.Clock (now() -> aware UTC)
    public_media: PublicMedia | None
    save_state: Callable[[dict], None]       # persist provider_state immediately (own transaction)
    before_commit: Callable[[dict], None]    # persist state + commit_phase=commit_sent BEFORE the irreversible call
    log: Callable[[str, str, dict | None], None]  # (event, spanish_message, data) -> post_events

@dataclass
class StepResult:
    status: Literal["continue", "wait", "published", "sent_to_app"]
    wait_seconds: int = 0             # for "wait": engine re-runs publish_step after this delay
    external_id: str | None = None
    external_url: str | None = None
    message: str | None = None        # Spanish, for post_events

@dataclass
class ReconcileResult:
    outcome: Literal["published", "not_published", "unknown"]
    external_id: str | None = None; external_url: str | None = None; message: str | None = None

@dataclass
class FormatSpec:
    key: str; label: str  # e.g. ("reel", "Reel")
    media: Literal["none_or_images", "images", "video", "image_or_video", "images_or_video"]

@dataclass
class OptionSpec: ...   # optional helper for templates; not required

@dataclass
class AccountSnapshot:  # analytics, one day
    day: date; metrics: dict[str, float | int]   # canonical keys, see section 12

@dataclass
class PostMetricData:
    external_id: str; published_at: datetime | None; permalink: str | None; caption: str | None
    media_type: str | None; thumbnail_url: str | None; metrics: dict[str, float | int]

@dataclass
class CommentData:
    external_id: str; parent_external_id: str | None; object_external_id: str
    object_permalink: str | None; author_name: str | None; author_id: str | None
    text: str; created_time: datetime; from_us: bool; raw: dict

class Provider(ABC):
    network: str; display_name: str; formats: list[FormatSpec]
    supports_comments: bool = False
    supports_analytics: bool = True

    def validate(self, target: TargetSpec) -> list[Issue]           # default: rules.validate_target(target)
    @abstractmethod
    def publish_step(self, ctx: PublishContext) -> StepResult        # may raise ProviderError
    def reconcile(self, ctx: PublishContext) -> ReconcileResult      # default: "unknown"
    def refresh_credentials(self, account: AccountCreds, settings: ProviderSettings,
                            http: httpx.Client, clock: Clock) -> TokenUpdate | None   # default None
    def check_account(self, account, settings, http) -> None        # raise ProviderError(AUTH) if broken
    def fetch_account_snapshot(self, account, settings, http, day: date) -> AccountSnapshot
    def fetch_recent_posts(self, account, settings, http, since: datetime) -> list[PostMetricData]
    def fetch_comments(self, account, settings, http, since: datetime) -> list[CommentData]
    def reply_comment(self, account, settings, http, comment_external_id: str, text: str) -> str
```

`providers/registry.py`: `get_provider(network) -> Provider`, `all_providers()`. Demo provider is registered
always; the UI only offers it when `demo_mode` is on.

**Engine/provider protocol** (section 10 details): the engine calls `publish_step` repeatedly. Each call does
the next unit of work based on `ctx.state`, calls `ctx.save_state` after anything that must survive a crash
(e.g. a created container id, an upload session id, a TikTok `publish_id`), and **must call
`ctx.before_commit(state)` immediately before the single irreversible request** (FB `/feed` or `/photos`
published, FB reels `finish`, IG `media_publish`, TikTok `video/init` for direct post/inbox — for TikTok the
init itself is the commit point: once a `publish_id` exists the post can go live, so save it right after the
response). Returning `wait` lets the engine sleep without blocking other targets (polling IG container status /
TikTok publish status). `publish_step` must be re-entrant from any persisted state.

`reconcile(ctx)` is called when the engine finds `commit_phase == commit_sent` without a final result (crash,
timeout, ambiguous error): check the remote side (IG container `status_code == PUBLISHED` → published; FB:
look up recent posts / video status; TikTok: `status/fetch` with the saved `publish_id`). Return `unknown` when
it cannot be determined — the engine then sets the target to `unknown` and notifies; it never re-sends.

Demo provider (`providers/demo.py`): no network calls. Behavior driven by `options` for tests/demo:
`demo_steps` (int, default 2 → one `wait` of 1 s then publish), `demo_fail` (`transient`|`permanent`|`auth`|
`ambiguous`|`rate_limited`|none), `demo_fail_times` (int). Produces `external_id="demo-<target_id>"` and
`external_url=None`. Snapshot/posts/comments return deterministic **clearly labeled demo data** derived from
the account id (only when demo_mode is on).

---

## 6. OAuth / connection contracts (module-level functions)

`providers/meta_common.py` (owned by the Facebook agent; used by Instagram agent and connections routes):

```python
GRAPH = "https://graph.facebook.com"
def graph_url(settings, path) -> str
def graph_request(http, method, url, *, params=None, data=None, files=None, headers=None,
                  timeout=60.0) -> dict   # raises ProviderError via classify_meta_error (HTTP 200 + "error" too)
def classify_meta_error(status_code: int, body: dict | None, headers) -> ProviderError
SCOPES_FB_IG: list[str]   # pages_show_list, pages_manage_posts, pages_read_engagement, read_insights,
                          # pages_manage_engagement, pages_read_user_content, instagram_basic,
                          # instagram_content_publish, instagram_manage_insights, instagram_manage_comments,
                          # business_management
def build_login_url(settings, redirect_uri: str, state: str) -> str
def exchange_code(settings, http, code: str, redirect_uri: str) -> tuple[str, datetime | None]
    # short-lived -> long-lived user token; returns (user_token, expires_at)
@dataclass class MetaIgAccount: id, username, name, picture_url, account_type (if known)
@dataclass class MetaPage: id, name, access_token, tasks: list[str], picture_url, instagram: MetaIgAccount|None
def list_pages(settings, http, user_token: str) -> list[MetaPage]   # /me/accounts with pagination, fields
def redirect_uri(settings) -> str   # f"{settings.base_url}/conexiones/callback/meta"
```

`providers/tiktok.py` (owned by the TikTok agent):

```python
def redirect_uri(settings) -> str   # f"{settings.base_url}/conexiones/callback/social1"
def new_pkce_pair(encoding: str) -> tuple[str, str]   # (verifier, challenge); "hex" default
def build_login_url(settings, redirect_uri, state, code_challenge) -> str
def exchange_code(settings, http, code, redirect_uri, code_verifier) -> TikTokTokens
@dataclass class TikTokTokens: open_id, access_token, expires_at, refresh_token, refresh_expires_at, scopes
def get_user_info(settings, http, access_token) -> dict   # display_name, avatar_url, username, ...
@dataclass class CreatorInfo: nickname, username, avatar_url, privacy_level_options: list[str],
    comment_disabled, duet_disabled, stitch_disabled, max_video_post_duration_sec: int | None
def query_creator_info(account: AccountCreds, settings, http) -> CreatorInfo
def revoke(settings, http, access_token) -> None
```

Connection routes (`routes/connections.py`, owned by the Connections agent) orchestrate: create
`oauth_states` row → redirect → callback validates state (and expiry) → exchange → (Meta) page chooser page
listing pages with their linked IG accounts and warnings (missing `CREATE_CONTENT`, IG not business) → create or
update `accounts` rows for the active brand (encrypt tokens via crypto). TikTok: callback → exchange →
`get_user_info` → create/update account. Disconnect: mark `disconnected`, wipe tokens (TikTok: call `revoke`).

---

## 7. Formats, options and validation (`rules.py`)

`rules.validate_target(target: TargetSpec) -> list[Issue]` and
`rules.FORMATS: dict[network, list[FormatSpec]]`, `rules.default_options(network, format) -> dict`,
`rules.char_limit(network, format) -> int`. Data-driven limits table (constants at the top of the module, with a
comment citing the research doc section). Spanish messages.

Formats and options (option keys are part of the contract; providers read them from `target.options`):

- **facebook**: `post` (text and/or 1–10 images, or 1 video), `reel` (1 video, 3–90 s, vertical recommended),
  `story` (1 image or 1 video ≤60 s).
  Options: `title` (video/reel title, optional), `link` (url, post only, no media), `native_schedule` (bool —
  delegate scheduling to Facebook so it publishes even if the PC is off; only for `post` and when
  scheduled ≥ 15 min and ≤ 29 days ahead).
- **instagram**: `post` (1 image → IMAGE; 2–10 images/videos → CAROUSEL; 1 video → published as REELS with
  `share_to_feed`), `reel` (1 video 3 s–15 min), `story` (1 image or 1 video ≤60 s; **business accounts only**).
  Options: `first_comment` (post/reel), `collaborators` (list of usernames, max 5, post/reel/carousel; not story),
  `share_to_feed` (reel, default true), `thumb_offset_ms` (reel cover frame), `alt_text` (image post),
  `trial_reel` (`null`|`MANUAL`|`SS_PERFORMANCE`), `location_id` (optional raw id). Images are converted to JPEG
  ≤ 8 MB at publish time; aspect ratio 4:5 – 1.91:1 for feed images (warning + offer to pad), 9:16 for
  reels/stories (warning).
- **tiktok**: `video` (1 video; duration ≤ creator `max_video_post_duration_sec`, MP4/MOV/WebM, ≥ 360 px).
  `photo` is listed but **disabled** in v1 (requires a verified public domain) — shown greyed with explanation.
  Options: `mode` (`direct` | `inbox`; inbox = "Enviar a la app de TikTok como borrador"), `privacy_level`
  (REQUIRED, **no default**, one of creator `privacy_level_options`), `disable_comment`, `disable_duet`,
  `disable_stitch` (booleans; UI shows "Permitir comentarios/Dúo/Stitch" unchecked by default), `is_aigc`
  (bool), `brand_organic_toggle`, `brand_content_toggle` (commercial disclosure; branded content cannot be
  `SELF_ONLY`), `video_cover_timestamp_ms`, `consent` (bool, must be true for direct post — the UI shows TikTok's
  required consent sentence). Text: caption ≤ 2200 chars; newlines allowed but warn they may be flattened.
- **demo**: `post` (anything). Options: see demo provider.

Validation also emits **warnings** for: HEVC/HDR video (may be rejected), very large files, missing text,
TikTok unaudited note when `privacy_level_options == ["SELF_ONLY"]`, Instagram image needing public URL (when
public media mode is off → error: "Activa la URL pública en Ajustes para publicar fotos en Instagram").

Global composer rule: each selected target must pass validation (no `error` issues) before "Programar" or
"Publicar ahora" is accepted (server re-validates on save). Drafts/ideas may be saved with errors.

Character counting: Python `len()` for FB/IG; TikTok counts UTF-16 code units.

---

## 8. Media (`media.py`)

- `ingest(db, file_obj, original_name, brand_id) -> MediaAsset`: stream to temp, sha256, dedupe (return existing
  non-deleted asset), sniff type (Pillow / PyAV, not by extension only), store under
  `media/originals/<sha[:2]>/<sha>.<ext>`, extract metadata (Pillow: size, EXIF orientation; PyAV: duration, w/h
  after rotation, fps, codecs, bitrate, HDR detection via color transfer `smpte2084`/`arib-std-b67`), make a
  thumbnail (`media/thumbs/<sha>.jpg`, max 480 px; video poster frame at 1 s or 10%).
- Allowed: JPEG, PNG, WebP, GIF (first frame for thumb), HEIC (if pillow-heif present), MP4, MOV, WebM, M4V.
  Max upload 4 GB (stream; never read whole file in memory).
- `derived_jpeg(asset, max_bytes=8_000_000) -> Path`: sRGB JPEG for Instagram (cached in `media/derived`).
- `to_media_info(asset) -> MediaInfo`.
- Library UI: grid with thumbnails, type filter, search by name, upload (drag & drop, multiple, progress via
  htmx/XHR), detail panel with metadata and per-network compatibility badges (from `rules`), delete (soft; blocked
  if used by scheduled targets).

---

## 9. Public media (`publicmedia.py`) — for Instagram images (and any provider needing a public URL)

`PublicMediaService` implementing `PublicMedia`:
- Modes (setting `public_media_mode`): `off` | `tunnel` (Cloudflare quick tunnel) | `base_url` (user supplies a
  public HTTPS base URL that proxies to our public media port, e.g. their own named tunnel).
- Public media server: a minimal separate ASGI app on `127.0.0.1:<port+1>` (default 8766) serving **only**
  `GET /m/<token>` from `public_media_tokens` (unexpired, not revoked) with correct `Content-Type`,
  `Content-Length`, range support not required; 404 otherwise; no directory listing; no other routes.
  Run in a background thread with Uvicorn (`install_signal_handlers` disabled / `config.setup_event_loop` safe).
- Tunnel: manage `cloudflared` binary in `<data>/bin/` — download a pinned release from GitHub
  (`cloudflared-windows-amd64.exe` / linux / darwin) **only after the user enables tunnel mode in Ajustes**
  (explicit consent), verify SHA-256 against a pinned table in code; start
  `cloudflared tunnel --url http://127.0.0.1:<pubport> --no-autoupdate --metrics 127.0.0.1:<mport>`; read the
  `https://*.trycloudflare.com` hostname from the metrics `/quicktunnel` endpoint (fallback: parse stderr);
  keep alive while tokens are active; stop after 5 idle minutes; restart on failure.
- `url_for(path, mime, ttl)` creates a token (128-bit), ensures server + tunnel are up, returns
  `https://<host>/m/<token>`. `release(url)` revokes the token.
- If mode is `off`, `url_for` raises `ProviderError(PERMANENT, "public_media_off", "Para publicar fotos en
  Instagram activa la URL pública en Ajustes.")`.

---

## 10. Publishing engine (`engine.py`) and scheduler (`scheduler.py`)

Scheduler: a daemon thread started in the FastAPI lifespan (skippable via `PLANIFICADOR_NO_SCHEDULER=1` for
tests). Tick every 15 s or when woken (`scheduler.wake()` after "Publicar ahora"/save). Each tick:

1. **Due targets**: `status == scheduled AND scheduled_at <= now` (and post not deleted):
   - `auto_publish == False` (and not TikTok inbox mode) → set `manual_pending`, notify (Publicación asistida).
   - else, if `now - scheduled_at > grace` (setting `grace_minutes`, default 120) and the app was not running at
     that time → `missed` + notification (actions: Publicar ahora / Reprogramar / Descartar).
   - else claim (atomic `UPDATE ... SET status='publishing', claimed_at=now WHERE id=? AND status='scheduled'`
     — check rowcount) and run.
2. **Waiting targets**: `status == publishing AND next_attempt_at <= now` → continue running.
3. **Stale claims**: `publishing` with `claimed_at` older than 10 min and no `next_attempt_at` (crash) →
   if `commit_phase == commit_sent` → reconcile; else continue from saved state.
4. **Periodic jobs** (tracked in settings with last-run timestamps): token refresh check (hourly; TikTok refresh
   when < 1 h left; Meta long-lived user token expiry warnings), analytics snapshot (daily after 06:00 local and
   at startup if stale), comment polling (every 15 min), trash purge (> 45 days), public media token cleanup.
5. **Sleep/wake detection**: if the wall-clock gap between ticks > 2 min, log and run catch-up.

Runner for one target (`engine.run_target(target_id)`), in a worker thread pool (max 2 concurrent publishes; at
most 1 per account):
- Build `PublishContext` (decrypt tokens, `rules`/`media` to MediaInfo, ProviderSettings, shared `httpx.Client`
  with sane timeouts: connect 15 s, read 120 s for uploads).
- Before the first step: refresh credentials if expiring; re-validate; failure → `error`.
- Loop: `result = provider.publish_step(ctx)`:
  - `continue` → loop immediately; `wait` → set `next_attempt_at = now + wait_seconds`, release worker;
  - `published` → `published`, store ids/url, `published_at`, event, notify only if it was "Publicar ahora"?
    (no: just event; errors and manual ones notify);
  - `sent_to_app` → `sent_to_app` + notification "Abre TikTok para terminar la publicación".
- `ProviderError` handling:
  - `TRANSIENT` / `RATE_LIMITED`: if `commit_phase == commit_sent` → reconcile first; else retry with backoff
    1, 5, 15, 30 min (+±20% jitter; `retry_after` wins) up to 4 attempts → then `error`.
  - `AUTH`: account → `needs_reconnect`, target → `error` with message, notification "Reconecta tu cuenta".
  - `PERMANENT`: `error`.
  - `AMBIGUOUS` or any exception after `before_commit`: reconcile → published / not_published (→ retry path) /
    unknown (→ `unknown` + notification; never auto-retried).
- Every state change writes a `post_events` row. Keep the PC awake while a publish is running (Windows:
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` via ctypes; no-op elsewhere).
- Manual actions (routes): Reintentar (from `error`/`missed`: reset to `scheduled` now; from `unknown` only after
  the user confirms it did NOT publish), Marcar como publicado (`manual_pending`/`unknown`/`error` →
  `manual_done`), Cancelar (→ `cancelled`), Publicar ahora (sets `scheduled_at=now`, `scheduled`, wake).
- Facebook `native_schedule`: provider publishes with `published=false` + `scheduled_publish_time`; result status
  `published` is only set at the scheduled time by a periodic check — v1: the provider returns
  `published` with `external_id` immediately and stores `state.native_scheduled = true`; the planner shows it
  as "Programado en Facebook". Cancelling such a target calls the provider's delete (best effort).

---

## 11. Notifications (`notify.py`)

`notify(db, kind, title, body, target_id=None, account_id=None)` → DB row + desktop notification
(`desktop-notifier` in a background thread; failures logged and ignored). The UI bell (top bar) polls
`/api/notificaciones/resumen` every 30 s (htmx) showing the unread count; the open tab also raises a browser
`Notification` for new `manual_publish`/`error` items (permission asked on first click of a "Activar avisos"
button). Notification center page lists all, mark read, deep-links to the target.

Publicación asistida modal (`GET /objetivos/{id}/asistida`): shows network, account, final text (per-network
override applied) with "Copiar texto", media list with "Descargar" (single files) / "Descargar todo (.zip)",
link "Abrir Instagram/TikTok/Facebook" (web URLs), and "Marcar como publicado".

---

## 12. Analytics (`analytics.py`)

Canonical snapshot metric keys (each provider fills what it can; missing = absent):
`followers`, `following`, `posts_count`, `views` (FB `page_media_view`, IG `views`), `reach`,
`interactions` (likes+comments+shares+saves), `profile_views`, `likes_total` (TikTok), `video_count` (TikTok).
Post metric keys: `views`, `reach`, `likes`, `comments`, `shares`, `saves`, `interactions`, `engagement`
(interactions / reach × 100, or / views when no reach).

- `snapshot_all(db, http, clock)`: for each connected account, `fetch_account_snapshot` for today (upsert) and
  `fetch_recent_posts(since=now-30d)` (upsert into `post_metrics`, link `target_id` by `external_id`).
  Errors are recorded per account (shown in UI), never crash the scheduler.
- Aggregations: `kpis(account, start, end)` → current vs previous period values and % change;
  `series(account, metric, start, end)`; `follower_balance`; `top_posts`; `best_times(brand)` → 7×24 matrix of
  average engagement of our published posts (needs ≥ 10 posts; else a default pattern labelled
  "Patrón general") used by the planner shading.
- UI (`/estadisticas`): left sidebar "Resumen" + one entry per connected account; date range presets (7, 28,
  90 días, personalizado); KPI cards with ▲▼ %; Chart.js line/bar charts (followers evolution, daily views/reach,
  interactions); post table (thumbnail, date, text, type, views, reach, likes, comments, shares, engagement) with
  column sort and **Descargar CSV**; "Actualizar ahora" button; empty states explaining data starts accumulating
  from the connection day. Charts must follow the `dataviz` skill guidance (light/dark tokens, accessible palette).

---

## 13. Comments inbox (`inbox.py`, `/comentarios`)

Poll FB/IG comments for posts published in the last 30 days (both ours and recent account posts) every 15 min
and on demand. List grouped by post with filters (red, no leídos, sin responder), reply box (htmx), mark as
read. Replies via provider `reply_comment`. TikTok: not supported (explain in UI).

---

## 14. CSV (`csvio.py`, `/importar`)

Template download (`plantilla.csv`, UTF-8 with BOM for Excel, `;` separator autodetect on import) with columns:
`fecha` (YYYY-MM-DD), `hora` (HH:MM, brand tz), `texto`, `facebook` (si/no), `instagram` (si/no),
`tiktok` (si/no), `formato_facebook`, `formato_instagram`, `formato_tiktok`, `medios` (file names in the
library separated by `|`), `privacidad_tiktok`, `primer_comentario`, `autopublicar` (si/no).
Import flow: upload → preview table with per-row validation (errors highlighted) → confirm creates posts
(status `draft` if errors, else `scheduled`). Export: `/exportar.csv` of posts in a date range.

---

## 15. UI / design system

**Layout fidelity: `docs/research/05-metricool-capturas.md` (real screenshots of Metricool from the user) is
binding for layout and flow** — planner sub-tabs + timezone indicator + one-row toolbar + week grid with
best-times shading; the composer as a full-screen overlay with editor card (network logo row with format chips,
"Editar por red social", text box with media thumbnails, icon toolbar, counters, collapsible "Configuración
global" with the Autopublicar toggle and per-network config sections, error panel, footer with Cancelar /
date-time / split "Programar") and preview card (network tabs, mobile/desktop toggle, realistic mocks, disclaimer);
analytics with left network sidebar, COMUNIDAD/CUENTA/POSTS tabs, period pickers and coloured KPI tiles; inbox
with two panels. Where 05 and the text below disagree on layout, 05 wins; colours/branding stay ours.

Own identity — never use Metricool's name, logo, colors or copy. App name in UI: **"Planificador"** with a
small original logo (inline SVG: rounded square with a calendar/orbit glyph).

- Layout (`base.html`): top bar (64 px) — left: logo + brand switcher dropdown (avatar dot color + name, list of
  brands, "Gestionar marcas"); center/left nav: **Planificación · Estadísticas · Comentarios · Biblioteca**;
  right: "Nueva publicación" primary button, notifications bell with badge, ☰ menu (Cuentas conectadas, Marcas,
  Importar/Exportar CSV, Ajustes). Content area full width with max 1440 px. Toast area bottom-right.
  Modal container for htmx-loaded modals (composer opens as a large modal; also reachable as a full page).
- CSS tokens in `:root` (light) and dark under `@media (prefers-color-scheme: dark)`: `--bg`, `--surface`,
  `--surface-2`, `--border`, `--text`, `--text-muted`, `--primary` (#5b4cf0), `--primary-contrast`,
  `--success`, `--warning`, `--danger`, `--info`, network colors `--fb` (#1877f2), `--ig` (#e1306c),
  `--tt` (#111 / dark: #f5f5f5), `--demo` (#8b5cf6). System font stack. 8 px spacing scale, radius 10 px,
  subtle shadows. Components: `.btn` (primary/secondary/ghost/danger, sizes), `.card`, `.badge` (status colors),
  `.chip`, `.tabs`, `.modal`, `.drawer` (right side panel for post detail), `.form-field`, `.switch`,
  `.dropdown`, `.table`, `.empty-state`, `.toast`, `.avatar`, `.net-icon` (network logo in colored circle).
- Status badge colors: published=success, scheduled=primary, draft=muted, publishing=info (spinner),
  manual_pending/sent_to_app=warning, error/unknown/missed=danger, cancelled=muted strike.
- Accessibility: visible focus rings, labels on inputs, aria attributes on dialogs/tabs, keyboard: `Esc` closes
  modals, `N` opens composer.
- Responsive down to 1024 px width (desktop app; mobile is not a goal but must not break).
- Planner calendar: FullCalendar with `timeZone: 'UTC'` and **floating wall times** — the server sends event
  start/end as brand-local wall time formatted as ISO without offset; drag & drop sends back wall time which the
  server interprets in the brand timezone. Event content: network icon(s), time, first 60 chars of text,
  thumbnail if media, status badge color stripe. Clicking a day slot opens the composer prefilled with that
  date/time. "Mejores horas" toggle adds background events per hour shaded by score.

---

## 16. Testing and quality bar

- `uv run pytest` must pass (unit + route tests). Each module ships tests: providers with respx mocks covering
  the full happy path + key error codes + reconcile; engine with the demo provider and a fake clock covering
  idempotency (no double commit on crash/timeout), retries/backoff, manual mode, missed, unknown; routes with
  FastAPI TestClient (CSRF honored via a helper); rules/media with generated images and a tiny generated video
  (PyAV can encode a 1 s test video in a fixture).
- `uv run ruff check` clean (config in pyproject: line length 110, target py312).
- No network access in tests (respx `assert_all_mocked`).
- Playwright e2e smoke (tests/e2e, marked `e2e`, run in integration phase with demo mode): open planner, create
  a demo post via composer, see it on the calendar, drag it, publish now, see published status; open library,
  analytics, inbox, settings pages without console errors.

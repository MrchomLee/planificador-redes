# 04 — Local architecture: scheduler, media, public URLs, secrets, security, frontend, Windows packaging, tests

Research date: **2026-10-15**. Target: "Planificador de Redes", a single-user web app that runs on the user's
**Windows PC** at `http://localhost`. Stack: Python 3.12+ managed with **uv**, FastAPI + Uvicorn, SQLite,
Jinja2 + HTMX + Alpine.js, vendored FullCalendar and Chart.js, and an in-process scheduler. The user brings their
own Meta and TikTok developer apps.

How to read this document

- **Ground truth** = three AGPL reference implementations, read only and never copied: `brightbean-studio`
  (Python/Django), `postiz-app` (TypeScript/NestJS + Temporal) and `openpost` (Go). Paths are relative to each
  repo root. Two more sources were inspected directly:
  - `cloudflared` at tag **2026.10.0** (Apache-2.0), shallow-cloned from GitHub.
  - Package files from **pypi.org** and **registry.npmjs.org**. The wheels and tarballs were downloaded and their
    file lists checked.
- Web facts came from search-result snippets of official docs. The session's web-search budget ran out before
  every point could be searched. Claims that rest on general knowledge and were not re-checked here are marked
  **(knowledge, not re-verified)**. Contradicted or unconfirmed claims are marked **UNVERIFIED**.
- This report complements `02-meta-api.md` (§4.3 and §8 already settle the Instagram image/tunnel question). Where
  this report refines 02, it says so.

---

## 0. Summary of decisions

| Topic | Decision |
|---|---|
| Scheduler | One process. A **DB-polling asyncio loop** starts in the FastAPI lifespan (tick 15 s). SQLite is the queue. **No APScheduler**: 3.11.3 is stable but would duplicate state, and 4.x is still alpha. A file lock enforces a single instance. |
| Never double-publish | Claim a row with a conditional `UPDATE … WHERE status='scheduled' RETURNING`. Persist a **write fence** (`stage='commit_sent'`) before every irreversible call. After a crash or an ambiguous error, **reconcile with the platform, never replay**. |
| Retries | Classify errors as permanent, auth, rate-limit or transient (before the fence), or ambiguous (after it). Back off at 1 min · 5 min · 15 min · 30 min with ±20 % jitter, max 4 attempts, and honour platform reset times. After 6 h without a verdict, ask the user. |
| Missed posts (PC off or asleep) | On start or wake, a due post publishes automatically if it is no later than the **grace window (default 2 h, configurable)**. Later posts go to `missed`, and the user picks: publish now, reschedule or discard. Wake is detected from wall-clock gaps between ticks. |
| SQLite | WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`. Writes are serialized by an in-process lock and use `BEGIN IMMEDIATE`. No transaction is open during network I/O. Daily `VACUUM INTO` backup. |
| Media | Files live under `%LOCALAPPDATA%\PlanificadorRedes\media\…` with server-generated names and SHA-256 dedup. Per-network renditions are cached. **Pillow 12.3.0** handles images (+ optional **pillow-heif 1.8.1** for iPhone HEIC). **PyAV (`av`) 19.0.1** handles video probe and poster frames (FFmpeg bundled, abi3 wheel = CPython 3.12/3.13/3.14 on win_amd64/arm64). No ffmpeg install. |
| Public URLs | Facebook and TikTok video never need one (they upload local bytes). IG images (and IG-Login video) use an **on-demand Cloudflare quick tunnel**. It points only at a **separate public-media app on its own port**, which serves random-token URLs (128 bit) that expire in 60 min and are revoked when the container is `FINISHED`. The tunnel is shut down after use. TikTok **photo** posts need a verified domain, so the quick tunnel is impossible there. |
| Secrets | **Hybrid**. One random master key goes in **Windows Credential Manager** via `keyring` 25.7.0. Tokens and app secrets are encrypted in SQLite with **Fernet** (`cryptography` 50.0.2), with a key-id prefix for rotation. Fallback: a DPAPI-protected key file. **`cryptography` ships no `win_arm64` wheel** (none from 46.0.7 to 50.0.2), so Windows-on-ARM PCs must run an x86_64 Python (`UV_PYTHON_ARCH=x86_64`, §8.1). |
| Local web security | Bind to `127.0.0.1` only, on a fixed port (OAuth redirects). `TrustedHostMiddleware` allows `localhost` and `127.0.0.1` (DNS-rebinding guard). **One canonical host (`localhost`) for the UI and every OAuth redirect URI.** Unsafe methods need an Origin/`Sec-Fetch-Site` check **plus** a CSRF token header. Session cookie is `HttpOnly; SameSite=Lax` (**not** `Strict`: a Strict cookie is not sent on the cross-site redirect back from facebook.com / tiktok.com, which would break every OAuth callback, §6.4). A Jupyter-style **launch token** opens the session. Local password is optional. |
| Frontend (vendored, no CDN) | FullCalendar **6.1.21** (`index.global.min.js`, MIT; 7.1.2 exists, see §7). Alpine **3.17.4** (`dist/cdn.min.js`, MIT). htmx **2.0.11** (`dist/htmx.min.js`, 0BSD; 4.0.0 is on the `next` tag). Chart.js **4.5.1** (`dist/chart.umd.min.js`, MIT). emoji-picker-element **1.29.1** (ESM, Apache-2.0) + emoji-picker-element-data **1.8.0** `es/cldr/data.json`. |
| Run on Windows | Install uv via `winget`. `iniciar.bat` → `uv run --frozen planificador` (with `UV_PYTHON_ARCH=x86_64` on ARM64 PCs), which waits until healthy and then opens the browser. "Iniciar con Windows" uses a **Startup-folder shortcut** (no admin), with Task Scheduler as an advanced option. `SetThreadExecutionState` keeps the PC awake while a publish runs. Notifications use **desktop-notifier 6.2.0** (WinRT toasts) + in-page toasts. **`tzdata` is a required dependency on Windows.** |
| Tests | pytest 9.1.1. **respx 0.23.1** mocks httpx 0.28.1. An injected `Clock` handles most tests, **time-machine 3.5.1** handles integration (it does not touch `time.monotonic`). **pytest-playwright 0.10.0 / playwright 1.64.0** runs smoke tests against a fake-provider mode. CI runs on `windows-latest`. |

---

## 1. How the reference implementations do it

### 1.1 Brightbean Studio (Python/Django) — the closest analogue

**Worker and queue**

- It uses **django-background-tasks** (a DB-table queue, `requirements.txt` "Background tasks").
- Two recurring tasks are registered idempotently on `post_migrate` (`apps/publisher/apps.py:9-36`,
  `apps/common/background.py:14-35`):
  - `run_publish_cycle` every **15 s**;
  - `confirm_pending_publishes` every **60 s** (`apps/publisher/tasks.py:10-16`).
- A custom `StudioWorker` keeps **two lanes**, one reserved for publishing and one for everything else. Long
  analytics jobs therefore can never delay a publish (`apps/common/worker.py:1-26,47-65,103-150`).
- On SIGTERM the worker stops admission but **never unlocks unfinished jobs**, "because the platform may already
  have accepted a publish" (`worker.py:144-150`).
- Process model: `Procfile` → web with gunicorn, plus `worker: python manage.py run_studio_worker --duration 3600`.
  A standalone `run_publisher` loop also exists (`apps/publisher/management/commands/run_publisher.py`).

**Publish engine** (`apps/publisher/engine.py`)

- *Due query* (`:405-426`):
  - `status=SCHEDULED` and `coalesce(platform.scheduled_at, post.scheduled_at) <= now`.
  - Rows parked on a retry backoff are excluded (`:419`), otherwise the backoff was "dead code".
  - Ordered by time, capped per cycle.
  - **No lateness limit**: anything overdue publishes.
- *Claim* (`:442-469`): `select_for_update` on all of the post's platform rows, then a flip to `PUBLISHING` with
  an explicit `updated_at`. The confirmation sweep later uses `updated_at` to tell a slow publish from a dead
  worker. The DB connection is released before minutes of upload I/O (`:475`, `apps/common/db.py:33-49`).
- *Concurrency*:
  - Thread pools capped at 4 posts and 6 platform publishes (`:178-203`). The caps are a database-connection
    budget: production went down on 2026-09-15 (`:166-177`).
  - Media is downloaded once per post through a locked shared cache (`:215-280`).
- *Retries*:
  - `RETRY_BACKOFF = [60, 300, 1800]`, `MAX_RETRIES = 3` (`:59,121`).
  - A provider's absolute `resets_at` (quota window) overrides the ladder (`:1077-1121`).
  - Errors with `retryable=False`, or a refused token that is still valid, fail at once (`:647-668,1375-1408`).
- *Async platforms (TikTok)*:
  - A successful upload keeps the row in `publishing` (`:586-603`), and the confirmation sweep settles it from
    `check_publish_status` (`:1161-1308`).
  - Timeouts: 15 min without a platform handle means "interrupted"; 30 min still processing means a timeout;
    **6 h** without being able to *ask* means "unconfirmed" (`:140-147`).
  - The sweep "**deliberately never re-publishes** … a duplicate video on a live account cannot be taken back"
    (`:1178-1182`).
- *Post-success cleanup* must never fall into the retry path, which would double-post (`:605-612,670-689`).
- *First comment*:
  - Separate bounded retries (`[120, 600, 1800]`, `:206-211`).
  - A clean 4xx is **not** retried. Instagram once answered a comment POST with 400 **and created the comment
    anyway**, so three retries left four comments (`:1427-1450`).
- *Failure state writes* use a guarded `UPDATE … WHERE status='publishing'`. The row count decides who settled the
  row (`:965-1041`).

**Rate limits**

- A `RateLimitState` row per account (remaining, window reset) is updated from response metadata
  (`apps/publisher/models.py:42-86`, `engine.py:1343-1356`).
- Static per-provider hints in `providers/types.py:168-172`:
  - Instagram: `publish_per_day=100` (`providers/instagram.py:173-179`);
  - TikTok: `5` (`providers/tiktok.py:177-182`);
  - Facebook: `4800` (`providers/facebook.py:165-171`).

**Provider abstraction**

- `providers/base.py`, an ABC `SocialProvider` with:
  - OAuth: `get_auth_url`, `exchange_code`, `refresh_token`;
  - `publish_post(access_token, PublishContent) -> PublishResult`;
  - `check_publish_status`, `publish_comment` and metrics methods.
- Two class flags:
  - `publish_is_async` ("handed over" ≠ "live");
  - `needs_local_media` (URL-only providers skip the download) (`:138-149`).
- HTTP goes through httpx. 429 maps to `RateLimitError`, every other ≥400 to `APIError(status_code)`
  (`:369-445`).
- Exceptions carry `retryable` and `resets_at` (`providers/exceptions.py:42-131`).

**Media**

- Files are stored at `upload_to="media_library/%Y/%m/"` and thumbnails at `media_library/thumbs/%Y/%m/`
  (`apps/media_library/models.py:123,135`).
- Uploads are validated by **magic-byte sniffing**. The stored extension is canonicalised from the sniffed MIME,
  which stops an HTML/PNG polyglot from being served as `text/html` (`validators.py:47-65,107-188`).
- Size caps: 20 MB per image, 1 GB per video (`:67-72`).
- Pillow decode is bounded:
  - `draft()` plus a 30 MP ceiling (`services.py:374-440`);
  - shrink first, then flatten alpha (`:462-508`).
- Video metadata and posters **shell out to system `ffprobe`/`ffmpeg`** (`services.py:511-595`). This is exactly
  the dependency we must avoid on Windows.
- Media referenced by scheduled posts cannot be deleted (`services.py:324-350`).

**Public media**

- Local storage serves `/media/` **unauthenticated**. It must be publicly reachable because Instagram, Threads,
  Facebook, Pinterest and others fetch attachment URLs server-side (`README.md:108,869`).
- S3/R2 uses presigned URLs that expire after **1 h** (`config/settings/base.py:228-229`).
- The dev settings trust ngrok/cloudflared origins for CSRF (`config/settings/development.py:8-27`).
- **Disagreement:** the IG review command says "Localhost or **short-TTL signed URLs will not work**"
  (`apps/social_accounts/management/commands/instagram_review_test_calls.py:47`), while OpenPost uses 15-min
  signed URLs in production (§1.3). Our resolution is a 60-min TTL plus explicit revocation (§4.4).

**TikTok:** prefers `FILE_UPLOAD`, because `PULL_FROM_URL` needs a TikTok-verified domain, which presigned
S3/R2 URLs cannot satisfy (`providers/tiktok.py:322-327`).

**Secrets**

- `EncryptedTextField` and `EncryptedJSONField` use **AES-256-GCM**. The key comes from `SECRET_KEY` through
  HKDF-SHA256 with the mandatory `ENCRYPTION_KEY_SALT`, and a 12-byte nonce is prepended
  (`apps/common/encryption.py:1-109`).
- Applied to OAuth tokens (`apps/social_accounts/models.py:59-66`) and app credentials
  (`apps/credentials/models.py:70-73`). The UI shows masked values (`:104-113`).

### 1.2 Postiz (NestJS + Temporal)

**Workers and queues**

- Each post is a **Temporal workflow**. The workflow id `post_<id>` with `workflowIdConflictPolicy: USE_EXISTING`
  is the idempotency key (`apps/orchestrator/src/activities/post.activity.ts:116-147`).
- There is one Temporal task queue per provider, sized by the provider's `maxConcurrentJob`
  (`libraries/nestjs-libraries/src/temporal/temporal.module.ts:43-73`). Examples: Facebook 500 and Instagram 400
  (`facebook.provider.ts:43`, `instagram.provider.ts:45`).

**Mutation safety** (`apps/orchestrator/src/workflows/post-workflows/post.workflow.v1.1.2.ts`)

- Irreversible activities (`postSocialPending`, `finalizePost`, `postComment`) run with **`maximumAttempts: 1`**.
  "A retried activity whose previous (timed-out) attempt still completed in the background would publish twice"
  (`:41-92`).
- Activities heartbeat every 15 s. A heartbeat timeout **with no heartbeat recorded** means "never started, safe
  to retry". Any other timeout means "outcome unknown" (`:23-27,258-279`).
- A `posted` flag ensures that later failures (DB update, notification) never re-run the publish (`:428-431,518-522`).
- Pending publishes are polled read-only every 20 s, up to 90 checks (`:115-118,334-417`). Without a verdict the
  user gets "**check your account before posting again to avoid duplicates**" (`:312-329`).

**Catch-up**

- An hourly sweep re-signals any post still in `QUEUE` whose `publishDate` falls in the **last 2 days**
  (`missing.post.workflow.ts:19-35`, `posts.repository.ts:36-64`).
- The workflow itself sleeps until `publishDate`, or 0 when overdue (`post.workflow.v1.1.2.ts:166-173`).

**HTTP retries:** the provider `fetch` retries 429, plain 500 and "rate limit" bodies after 5 s, up to 3 times. It
strips the query string from heartbeat details because tokens live there
(`libraries/nestjs-libraries/src/integrations/social.abstract.ts:563-660`).

**Media**

- Local storage writes `/{YYYY}/{MM}/{DD}/{32 random hex}.{ext}` under `UPLOAD_DIRECTORY`. The public URL is
  `FRONTEND_URL + /uploads/...`: unguessable, but **no expiry**.
- MIME is sniffed with `file-type` (`libraries/nestjs-libraries/src/upload/local.storage.ts:11-24,28-49,69-77`).
- Alternative: Cloudflare R2 (`docker-compose.yaml:19-30`).
- Server-side validation deliberately has "no ffmpeg dependency". Image dimensions come from `sharp`
  (`social.abstract.ts:183-200`).

**TikTok:** video moved to `FILE_UPLOAD`, with "OLD PULL_FROM_URL … kept for when the TikTok PULL bug is fixed".
"**TikTok photo posts only support PULL_FROM_URL, there is no FILE_UPLOAD**" (`tiktok.provider.ts:627,713,820-841`).

**Secrets:**

- `AuthService.fixedEncryption` is AES-256-CBC with key and IV derived from `JWT_SECRET` via MD5 `EVP_BytesToKey`.
  The IV is deterministic and nothing authenticates the ciphertext (`libraries/helpers/src/auth/auth.service.ts:6-58`).
- `Integration.token` is a plain `String` column (`schema.prisma:366-380`). No encryption call was found in the
  integrations repository (UNVERIFIED that tokens are never encrypted elsewhere).
- **Do not imitate.**

### 1.3 OpenPost (Go, SQLite by default)

**SQLite:** `SetMaxOpenConns(1)`, `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`
(`apps/server/internal/database/database.go:100-116`).

**Job table worker** (`apps/server/internal/queue/worker.go`)

- A ticker loop runs (`:451-481`), and stale jobs are requeued at the start of every cycle (`:524-541`).
- *Claim*: a single statement, `UPDATE jobs SET status='processing', locked_at=…, locked_by=? WHERE status='pending'
  AND id=(SELECT id … WHERE run_at<=now ORDER BY run_at LIMIT 1) RETURNING *`. Postgres uses
  `FOR UPDATE SKIP LOCKED` instead (`:648-691`).
- The lock heartbeats every `staleProcessingJobAge/3`. Stale age = 15 min (`:48-64,693-744`).
- Jobs whose provider write may have happened are **fenced as ambiguous and failed, not requeued** (`:543-646`).
- Completion is guarded by `WHERE id=? AND status='processing' AND locked_by=?` (`:721,733`).

**Retry delay:** `30 s·2^(n-1)`, capped at 15 min. A provider `retry-after` can raise it to 30 min. Jitter is ±20 %
(`internal/services/publisher/failures.go:297-313`, `worker.go:751-781`).

**Durable provider-write fence**

- Write states: `prepared → sending → accepted | definite_failure | ambiguous`. Delivery states include
  `processing`, `awaiting_user` (TikTok inbox) and `manual_resolution`
  (`internal/services/providerwrite/types.go:17-60`).
- Every adapter wraps its externally visible mutation in `executePublishWrite`, which persists `BeginWrite` before
  the call (`internal/platform/adapter.go:85-110`).
- Each result declares a **retry safety**: `safe` / `idempotent` / `reconcile_only` / `never`, with `never` as the
  default (`adapter.go:46-63`).
- Async platforms (Instagram, Facebook video, TikTok) return `reconcile_only` with a `ReconcileAfter` delay
  (`platform/instagram.go:869`, `facebook.go:912`, `tiktok.go:546,590`).

**Public media**

- **HMAC-SHA256 signed URLs** have the form `{base}/{mediaID}{.ext}?exp={unix}&sig={hex}`. The signature covers
  `"{mediaID}:{exp}"` (`internal/services/mediasigner/signer.go:16-40`, `internal/services/publicurl/media.go:98-139`).
- Publishing uses a **15-minute** lifetime (`publisher/publisher.go:2672-2680`, `publicurl/media.go:17,57`).
- The handler verifies in constant time and sets `Cache-Control: public, max-age=<remaining>`
  (`internal/api/handlers/media.go:4247-4256`).
- The signing key defaults to the encryption key (`internal/config/config.go:429-430`).
- Self-hosters are told to "expose OpenPost through a tunnel such as ngrok so the callback URL and `/media/...`
  paths are publicly reachable" (`docs/reference/providers/threads.md:43`, `troubleshooting.md:83`) and that "a
  tunnel can provide the public origin for a trial" (`apps/docs/content/docs/self-hosting/reverse-proxy.mdx:33-35`).

**Secrets:** AES-GCM, with a key-ID **envelope** (`OPENPOST\0` magic + version + key id) and a keyring of previous
keys for rotation (`internal/services/crypto/encrypt.go:16-60`).

### 1.4 What we take (ideas only, in our own code)

1. DB-as-queue with a short polling tick and a conditional-UPDATE claim (Brightbean, OpenPost).
2. A separate lane, or task, for non-publishing work (Brightbean).
3. A **write fence** plus "reconcile, never replay" (all three).
4. A bounded retry ladder that honours platform reset times (Brightbean, OpenPost).
5. A sweep that settles rows stuck in `publishing` (Brightbean confirm sweep, OpenPost stale fencing).
6. Magic-byte sniffing, canonical extensions and Pillow decode bounds (Brightbean).
7. Signed or expiring public media URLs (OpenPost), but behind an on-demand tunnel instead of a public server.
8. Authenticated encryption with a key id (OpenPost, Brightbean). Not Postiz's CBC with a fixed IV.

---

## 2. Scheduler and publishing engine

### 2.1 In-process loop vs separate worker → **in-process asyncio loop**

| Criterion | In-process (FastAPI lifespan task) | Separate worker process |
|---|---|---|
| Windows UX | One process, one console or tray. Closing it stops everything, which is clear. | Two processes to start, keep alive and stop. Orphan risk. |
| Robustness | A crash of the web server stops publishing too. A restart resumes from the DB (lease + fence). | Web crashes don't stop publishing, but on a desktop both usually die together (logoff, sleep, shutdown). |
| SQLite | One process: writes are serialized with an in-process lock, so there is no cross-process contention. | Two writers rely on `busy_timeout` and WAL. That works, but "database is locked" becomes possible. |
| Complexity | Low | Medium (IPC to nudge the worker after "publish now") |

**Decision:** run the engine as asyncio tasks started in the FastAPI `lifespan`, and cancel them on shutdown.

- Keep the engine a plain module (`engine.tick(now)`) callable from a CLI command too: `planificador publish-due
  --once`. This gives tests a hook, and a possible future headless Task-Scheduler mode.
- Never run with `--reload` or `workers>1` outside development, and guard with a **single-instance file lock**
  (`filelock` 4.2.0, MIT) on the data directory.
  - Uvicorn 0.54.0 picks `ProactorEventLoop` on Windows only when it does **not** use a subprocess. With
    reload or workers it uses `SelectorEventLoop` (`uvicorn/loops/asyncio.py:8-11`), which cannot run asyncio
    subprocesses on Windows.
  - Manage `cloudflared` with `subprocess.Popen` from a thread, so it works under either loop.
- Tasks inside the process:
  1. `publisher_loop` — tick every **15 s** (same as Brightbean, `apps/publisher/tasks.py:12`). It prepares and
     commits due targets.
  2. `reconcile_loop` — every 60 s. It settles `publishing`, `processing` and `ambiguous` rows, as in Brightbean's
     confirm sweep.
  3. `maintenance_loop` — every 60 s. It runs rows of a small `job` table with `next_run_at` (token refresh,
     analytics snapshot, comment polling, backups, media cleanup). Each job reschedules itself, like OpenPost's
     `Recurrence` (`worker.go:712-725`).
  - Read-heavy jobs never share a semaphore with publishing (Brightbean's lanes).
  - CPU-heavy media work (thumbnails, transcodes) runs in `asyncio.to_thread`, or in a `ProcessPoolExecutor` for
    transcodes, so the event loop never blocks.

### 2.2 Data model (proposal)

- `post` — the content master: caption, media list, author notes and the user's IANA time zone.
- `post_target` — one row per (post, social account). This is the unit of scheduling and status:
  - `status`: `draft | scheduled | preparing | publishing | processing | published | failed | missed |
    needs_review | awaiting_user | cancelled`;
  - `scheduled_at_utc` (TEXT ISO-8601 Z, or INTEGER epoch) plus `scheduled_tz`;
  - `attempt` and `next_attempt_at`;
  - `lease_owner` (process uuid) and `lease_until`;
  - `stage` (`prepared | uploaded | container_created | commit_sent | committed`);
  - `remote` JSON (container id, video id, publish_id, post id, permalink);
  - `last_error_code` and `last_error_user` (Spanish, user-facing), plus `published_at`.
  - Unique `(post_id, account_id)`.
- `publish_attempt` — an append-only log: target, attempt, stage, HTTP status, truncated body (≤1 KB, **tokens
  redacted**), duration. Same idea as Brightbean `PublishLog` (`apps/publisher/models.py:8-39`).
- `rate_state` — per account and network: the last usage headers (Meta `X-App-Usage` /
  `X-Business-Use-Case-Usage`, see 02 §7), the IG `content_publishing_limit` usage, and `blocked_until`.
- `job` — maintenance jobs: `name` (unique), `next_run_at`, `interval_s`, `last_ok_at`, `last_error`.
- `public_media_token` — see §4.4.

### 2.3 Claiming, idempotency and the write fence

1. **Due selection:** `status='scheduled' AND scheduled_at_utc <= now AND (next_attempt_at IS NULL OR
   next_attempt_at <= now)`. The second clause is Brightbean's lesson (`engine.py:414-419`).
2. **Claim**, one statement inside a `BEGIN IMMEDIATE` transaction:
   `UPDATE post_target SET status='preparing', lease_owner=:me, lease_until=:now+900, attempt=attempt+1 WHERE
   id=:id AND status='scheduled' RETURNING …`.
   - Proceed only if one row returns. RETURNING needs SQLite ≥3.35; check `sqlite3.sqlite_version` at startup.
   - Verified: CPython's Windows build bundles SQLite **3.49.1** on the 3.12 branch and **3.50.4** on 3.13/3.14
     (`PCbuild/get_externals.bat` in python/cpython). python-build-standalone (what uv installs) pins SQLite
     **3.53.1** in `pythonbuild/downloads.json`. Whether its Windows builds use that pin or CPython's externals was
     not checked, but either is far above 3.35. Keep the startup assertion as a guard.
3. **Two-phase publish per network.** Prepare is retry-safe; commit is not.
   - **Prepare** (may start ~2–5 min before T; report 02 §8 suggests T-2 min). Steps: refresh the token if
     needed, build renditions, upload bytes, create IG containers, wait for IG/FB processing. Each step persists
     `stage` and `remote` immediately.
     - An IG container expires 24 h after creation if not published (official IG Container reference via search),
       so never prepare far ahead.
   - **Commit** (at T): set `stage='commit_sent'` and `commit_sent_at=now` **in its own committed transaction**,
     then call the irreversible endpoint: IG `media_publish`, FB `/feed` / `/photos` / video finish, TikTok
     `publish/video/init` with the `FILE_UPLOAD` upload. Then persist the remote id and set `stage='committed'`.
     - **TikTok exception:** persist the `publish_id` returned by `video/init` **before** uploading the chunks. It is
       the only handle `status/fetch` accepts, and TikTok starts publishing on its own once the last chunk arrives.
       OpenPost checkpoints the `publish_id` and treats the whole init+upload as one `reconcile_only` write
       (`internal/platform/tiktok.go:536-546`).
     - Direct Post can end as `SEND_TO_USER_INBOX`: the video becomes a draft only the creator can publish. Map it
       to `awaiting_user` (Brightbean `providers/tiktok.py:89-96`).
4. **Ambiguity rule.** A timeout, connection reset or 5xx after `commit_sent` is **never retried blindly**. The row
   goes to `processing`/`needs_review`, and the reconcile loop asks the platform:
   - IG: `GET /{container-id}?fields=status_code` → `PUBLISHED` means done. OpenPost reconciles the same way
     (`internal/platform/instagram.go:1125-1139`). OpenPost then records an "unknown" media id, which suggests the
     status call does not carry it (UNVERIFIED). If a permalink is needed, look the post up in
     `GET /{ig-user-id}/media`;
   - FB video: `status.publishing_phase`;
   - TikTok: `POST /v2/post/publish/status/fetch/` with the stored `publish_id`;
   - FB feed or photo: list recent Page posts after `commit_sent_at` and match on a hash of the message plus the
     media (details in 02).
   - Only "definitely not published" re-opens the row for retry. This matches Brightbean
     (`engine.py:1178-1182,1411-1424`), Postiz (`post.workflow.v1.1.2.ts:74-92,312-329`) and OpenPost
     (`providerwrite/types.go`, `adapter.go:46-63`).
5. **Restart recovery.** The single-instance lock guarantees that any row in `preparing`/`publishing` at startup
   belongs to a dead process. On boot, without waiting for the lease to expire:
   - `stage < commit_sent` → back to `scheduled`, with retry-safe re-preparation (an orphan IG container simply
     expires);
   - `stage = commit_sent` → reconcile, as in point 4.
6. **Post-success side effects** (notification, first comment, analytics seed) run *after* `committed` is stored.
   They must never route into the publish retry path (Brightbean `engine.py:605-612`, Postiz `:428-431,518-522`).
7. **First comment:** separate status and retries. Retry only on 429, 5xx or a transport error, after checking the
   comment does not exist yet. Never retry a clean 4xx (Brightbean `engine.py:1427-1450`).

### 2.4 Retry policy

| Class | Examples | Action |
|---|---|---|
| Permanent / validation | Meta 100 and IG 2207xxx media-spec errors; TikTok `invalid_params`, `unaudited_client_can_only_post_to_private_accounts`, `spam_risk_text`, `spam_risk_user_banned_from_posting`, `url_ownership_unverified` | `failed`, with a Spanish message and the raw log. No retry. |
| Auth | Meta 190 / OAuthException; TikTok `access_token_invalid` | Refresh once if the network supports it (TikTok refresh token, IG-Login refresh), then retry *if pre-commit*. Otherwise mark the account "Reconectar" and fail the target. |
| Rate limit / quota | HTTP 429; Meta 4, 17, 32, 613; IG `content_publishing_limit` reached; TikTok `rate_limit_exceeded`; TikTok **`spam_risk_too_many_posts`** (the creator's daily Direct Post cap: retry the next day, Postiz `tiktok.provider.ts:187-193`) | Set `next_attempt_at` to the reset time when known (Brightbean `engine.py:1092-1106`). Otherwise use the ladder. Does not consume an attempt if the platform gave a reset time (OpenPost `preserveAttempts`, `worker.go:758-765`). |
| Needs the user | TikTok `spam_risk_too_many_pending_share`: at most **5 pending inbox shares per 24 h**. It applies to inbox/draft uploads, not Direct Post (03, limits and error tables; Postiz `tiktok.provider.ts:195-200`) | `awaiting_user`: "Termina o borra los borradores pendientes en la app de TikTok". Do not auto-retry before 24 h. |
| Transient, pre-commit | DNS failure, connect timeout, 5xx on upload or container creation | Ladder: **60 s, 5 min, 15 min, 30 min** with ±20 % jitter; max **4** attempts. Then `failed` with "Reintentar". |
| Ambiguous, post-commit | Timeout or 5xx on `media_publish` / `/feed` | **Reconcile only.** Give up after **6 h** → `needs_review`: "Puede que se haya publicado; revisa tu cuenta antes de reintentar" (Brightbean `engine.py:142-147,1310-1328`; Postiz `:312-329`). |

### 2.5 Missed jobs, PC sleep, restarts (catch-up policy)

**Facts**

- When Windows sleeps or hibernates, the process is suspended and nothing runs.
- Python ≥3.13 on Windows bases `time.monotonic()` on `QueryPerformanceCounter` (was `GetTickCount64`) (CPython
  gh-88494, via search). Whether that clock counts suspended time is **UNVERIFIED**. asyncio timers use the
  monotonic clock, so a long `asyncio.sleep(until_T)` may fire late or early relative to wall time after resume.
- **Design consequence:** never sleep until T. Tick every 15 s and compare `datetime.now(UTC)` with
  `scheduled_at_utc` from the DB.

**Wake detection:** if the wall-clock gap between two ticks is more than 3× the interval, log `resume_detected`,
re-run the refresh-token pre-flight for the next hour's posts, and run the catch-up pass.

**Catch-up policy** (on startup and after resume):

- `lateness = now - scheduled_at`.
  - `lateness <= grace` (setting "Publicar publicaciones atrasadas hasta X después", default **2 h**, range
    0–48 h) → publish now, with a note "publicada con N min de retraso".
  - `lateness > grace` → `missed`. Show a notification and a banner listing them, with buttons
    **Publicar ahora / Reprogramar / Descartar**.
- How the references compare:
  - Brightbean publishes everything overdue (`engine.py:405-426`).
  - Postiz re-dispatches anything up to **2 days** late (`posts.repository.ts:45-48`).
  - For social content (time-sensitive captions such as "buenos días") an explicit grace window is safer. The
    default value is a product decision; open question 1.
- Optional per-post flag "No publicar si se retrasa".

**PC off at T** — nothing can publish. Mitigations:

- Facebook only: an opt-in "programar en Facebook" mode that hands the post to Meta's native scheduling
  (`published=false` + `scheduled_publish_time`, see 02 §3.7). The PC then needs to be on only at hand-off time.
  Instagram and TikTok have no API-native scheduling in the references (UNVERIFIED for TikTok), so they need the PC
  on and the app running.
- UI: a permanent badge "Próxima publicación: hoy 18:30 — deja el PC encendido", and a warning when closing the app
  with posts due in the next 24 h.

**Keep awake while publishing:**

- Call `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` through `ctypes` while any target is in
  `preparing`/`publishing`, then clear it with `ES_CONTINUOUS`.
- Optional setting: hold it from T-10 min if a post is due.
- It prevents **idle** sleep only. Microsoft's reference says it "cannot be used to prevent the user from putting
  the computer to sleep", for example by closing the lid or pressing the power button (MicrosoftDocs/sdk-api
  `nf-winbase-setthreadexecutionstate.md`, Remarks). The flags belong to the calling thread, so set and clear them
  from one long-lived thread (the event-loop thread, or a dedicated one), never from a short-lived pool thread.

**Wake from sleep (advanced, opt-in):**

- Create a Task Scheduler task with `WakeToRun` for the next due time. It runs `iniciar.bat --headless`, or does
  nothing if the app is already running.
- Microsoft Q&A reports say Modern Standby (S0) machines often ignore wake timers. Diagnose with `powercfg /a`.
- Treat this as best-effort; never promise it (§8.4).

### 2.6 Rate limits and concurrency

- Global publish concurrency: a **2–3** semaphore, plus **1 per account** to preserve order and per-account limits.
  Video uploads run one at a time per network, because a home uplink saturates.
  - Postiz uses huge per-provider caps (FB 500, IG 400, TikTok 10000) because it is multi-tenant. Brightbean caps
    at 4 posts × 6 platforms for DB reasons. A single user needs neither.
- Reads (analytics, comments) go through a per-account token bucket (`aiolimiter` 1.3.0, MIT).
  - Meta: parse the usage headers and back off at >80 % (02 §7).
  - IG: check `GET /{ig-user-id}/content_publishing_limit` in pre-flight. Brightbean assumes 100 posts / 24 h
    (`providers/instagram.py:173-179`).
  - TikTok: query `creator_info` before each post (privacy options, max duration). Brightbean calls
    `_check_creator_constraints` (`providers/tiktok.py:315-320`).
- One shared `httpx.AsyncClient` per network, with timeouts: connect 10 s, read 60 s, write 300 s for uploads.

### 2.7 SQLite settings (FastAPI + background tasks in one process)

```text
PRAGMA journal_mode=WAL;          -- persistent (stored in the file); set once at init
PRAGMA synchronous=NORMAL;        -- per connection; durable enough in WAL (a power cut can lose the last txn, never corrupts)
PRAGMA busy_timeout=5000;         -- per connection (OpenPost uses 5000, database.go:108)
PRAGMA foreign_keys=ON;           -- per connection
PRAGMA temp_store=MEMORY;
PRAGMA journal_size_limit=67108864;
```

- **Python driver:**
  - Use `sqlite3.connect(path, timeout=5, check_same_thread=False, isolation_level=None)`, i.e. autocommit, with
    transactions opened explicitly.
  - Writes run under a process-wide `threading.Lock` and start with **`BEGIN IMMEDIATE`**.
    - In WAL, a deferred transaction that read first and then tries to write can get `SQLITE_BUSY` (snapshot)
      **without** waiting for `busy_timeout`. `BEGIN IMMEDIATE` takes the write lock up front (SQLite docs;
      knowledge, not re-verified).
  - With SQLAlchemy 2.1.4, set the same pragmas and the `BEGIN` from a `connect` / `begin` event listener.
- **Transaction scope:**
  - Never hold a transaction or connection across HTTP calls or uploads (the Brightbean lesson, `apps/common/db.py`).
  - Pattern: short read → network I/O → short guarded write (`WHERE status=… AND lease_owner=:me`), checking the
    row count.
- **Checkpoints and backups:**
  - Run `PRAGMA wal_checkpoint(TRUNCATE)` on clean shutdown and daily.
  - Daily backup with `VACUUM INTO 'backups\app-YYYYMMDD.db'`, keep 7. Never copy the `.db` file while the app runs
    (the `-wal` holds recent commits).
- **Location:** keep the database in `%LOCALAPPDATA%` and never in OneDrive-synced folders (Documents, Desktop).
  WAL needs a local NTFS volume with shared memory, and sync clients lock files (knowledge, not re-verified).

### 2.8 APScheduler — evaluated, not adopted

- APScheduler **3.11.3** (MIT, 2026-06-28; deps `tzlocal`) offers `misfire_grace_time`, `coalesce`,
  `max_instances` and a SQLAlchemy job store. **4.0** is still **alpha** (latest 4.0.0a6, 2025-04-27).
- Our schedule already lives in `post_target`. Mirroring each post into an APScheduler job store means two sources
  of truth, and every edit, drag-and-drop or deletion must also reschedule the job.
- The references all poll their own tables (Brightbean 15 s, OpenPost ticker, Postiz's hourly missed sweep).
- A 15-s DB poll costs nothing at our scale and survives restarts by construction.

### 2.9 Time zones (Windows gotcha)

- **Windows has no IANA tz database**, so `zoneinfo.ZoneInfo("Europe/Madrid")` fails unless the **`tzdata`**
  package is installed. Make `tzdata` (2026.5) an explicit dependency; Brightbean pins it for slim images,
  `requirements.txt`.
- Detect the default zone with `tzlocal` 5.4.4, which maps Windows zone names to IANA. Also accept the browser's
  `Intl.DateTimeFormat().resolvedOptions().timeZone`.
- Store UTC plus the IANA zone. Convert at the edges, and handle DST gaps and folds when the user picks a local
  time (02:30 on the spring-forward day: reject the time or shift it forward, and say so).

---

## 3. Media

### 3.1 Storage layout (via `platformdirs` 4.13.0 → `%LOCALAPPDATA%\PlanificadorRedes`)

```text
%LOCALAPPDATA%\PlanificadorRedes\
  data\app.db (+ -wal, -shm)
  media\originals\2026\10\<uuid>.<canonical-ext>      # server-chosen names; user filename kept only in DB
  media\derived\<asset-uuid>\thumb-400.jpg            # library thumbnail
  media\derived\<asset-uuid>\poster-<ms>.jpg          # video poster / cover candidates
  media\derived\<asset-uuid>\ig-feed-1080x1350.jpg    # per-network renditions, key = hash(params)
  media\tmp\                                         # in-flight uploads; wiped at startup
  backups\  logs\  bin\cloudflared-2026.10.0.exe  app.lock  runtime.json (port, pid)
```

- **Deduplicate** on a SHA-256 of the content (unique index) and keep reference counts. As in Brightbean
  (`services.py:324-350`), do not delete media referenced by scheduled or publishing posts. Run an orphan sweep
  weekly.
- Keep paths short (MAX_PATH 260) and never put user-supplied names in paths (Brightbean
  `validators.py:80-104`).
- Antivirus or indexers can briefly lock new files. Retry `os.replace` and unlink a few times on `PermissionError`
  (knowledge, not re-verified).

### 3.2 Upload validation

- Sniff magic bytes and ignore the client `Content-Type` and extension. Allow JPEG, PNG, WebP, GIF, HEIC (if
  pillow-heif is installed), MP4 and MOV. Store under the canonical extension (Brightbean `validators.py:47-65,
  112-156`; Postiz `local.storage.ts:69-77`). `filetype` 1.2.0 (MIT, pure) works, but an own 30-line sniffer is
  enough.
- Caps: images ≤ 30 MB and ≤ 40 MP after `draft()`; video ≤ 1 GB. Brightbean uses 20 MB / 1 GB
  (`validators.py:67-72`) and a 30 MP ceiling (`services.py:412`).

### 3.3 Images: Pillow 12.3.0 (MIT-CMU)

- **Windows wheels** cover cp310–cp315 for win32, amd64 and arm64.
- Pipeline:
  1. `Image.open` (header only), then `img.draft("RGB", 2×target)` for JPEG, then the pixel-ceiling check.
  2. `ImageOps.exif_transpose` (phone orientation).
  3. Convert the ICC profile to sRGB (`ImageCms`; Display-P3 iPhone photos).
  4. Shrink, then flatten alpha onto white (Brightbean's order, `services.py:462-508`).
  5. Save JPEG, quality ≈ 90.
- Per-network renditions:
  - **IG feed:** JPEG only, ≤ 8 MB, aspect **4:5 … 1.91:1**. Default 1080×1350. Offer "recortar / rellenar".
  - **IG Story / Reel cover:** 9:16.
  - **FB photo:** ≤ 4 MB (02 §3–§4).
  - The previews in the composer reuse the same renditions.
- **HEIC:** **pillow-heif 1.8.1** (BSD-3-Clause wrapper; wheels cp310–cp315 for win amd64 and arm64;
  `register_heif_opener()`). Licences of the bundled libheif and codec binaries were not checked (UNVERIFIED);
  decode-only use.

### 3.4 Video metadata and poster frames without installing ffmpeg — package evaluation

| Package (latest on PyPI, checked 2026-10-15) | What the Windows wheel contains | Metadata | Poster frame | Verdict |
|---|---|---|---|---|
| **av (PyAV) 19.0.1** (2026-10-03, BSD-3-Clause, `Requires-Python >=3.12`) | `av-19.0.1-cp312-abi3-win_amd64.whl` (28.1 MB) and `…-win_arm64` (20.7 MB). **abi3 = one wheel for CPython 3.12, 3.13, 3.14 (and 3.15)**, plus `cp314t` free-threaded wheels. Bundles FFmpeg 9.x DLLs in `av.libs/`: avcodec-63, avformat-63, swscale, dav1d, libx264, libx265, libvpx, opus… | In-process: duration, width/height, codec, fps, bit rate, audio codec, rotation side data | Decode a frame and call `frame.to_image()` to get a Pillow image | **Chosen.** One dependency gives probe + thumbnails (+ transcoding later), with no subprocess or console windows. |
| imageio-ffmpeg 0.6.0 (2025-01-16, BSD-2-Clause) | `py3-none-win_amd64` (31.2 MB) with **`ffmpeg-win-x86_64-v7.1.exe` (87.6 MB)**, a static build configured `--enable-gpl --enable-version3` (GPLv3 binary), **no ffprobe** | Only by parsing `ffmpeg -i` stderr (`imageio_ffmpeg.count_frames_and_secs`, read_frames meta) | `ffmpeg -ss 1 -frames:v 1` subprocess | Fallback for heavy CLI transcodes only. Older (Jan 2025), and subprocess-based. |
| pymediainfo 7.0.1 (2025-02-12, MIT) | `py3-none-win_amd64` (3.3 MB) with `MediaInfo.dll` (7.9 MB) | Very complete and fast (codec, profile, HDR, rotation, faststart hints) | **None** (no decoder) | Good lightweight probe, but posters still need a decoder. Not needed if we use PyAV. |

- Verification method: the PyPI JSON plus the downloaded wheels. avcodec's DLL embeds "libavcodec license: LGPL
  version 3 or later", yet the wheel also ships `libx264`/`libx265` DLLs, which are GPL projects.
  - For a personal tool installed from PyPI this does not matter.
  - If we ever ship a frozen installer that bundles them, review the licences (UNVERIFIED).
- **Poster recipe:**
  1. `av.open(path)`.
  2. Seek to `min(1 s, 10 % of duration)` on the video stream and decode the first frame after the seek.
  3. `to_image()` → Pillow thumbnail at 400 px, saved as JPEG.
  4. For the Reels cover picker, extract 8–12 evenly spaced frames on demand.
- Apply the display-matrix rotation. av 19.0.1 exposes it as the `VideoFrame.rotation` property (an int, in
  degrees; `av/video/frame.pyi:69-70` in the wheel), and streams have `set_display_rotation()` for encoding. The
  sign convention is still to be tested with portrait phone videos.
- **Checks against platform specs** (limits in 02 §3–§4 and the TikTok report):
  - container and codec (H.264/HEVC + AAC), fps, resolution, aspect ratio, duration, size, bit rate;
  - "moov before mdat" (faststart). Read the top-level MP4 box order directly; it is about 20 lines of Python.

### 3.5 Transcoding (only when a check fails)

- PyAV can encode H.264 + AAC (the libx264 DLL is in the wheel). Do it in a separate process: CPU-bound, and FFmpeg
  holds the GIL in places (UNVERIFIED).
- Typical fixes:
  - HEVC → H.264 for older targets;
  - VFR → CFR 30 fps;
  - fps > 60 → 60;
  - add faststart;
  - downscale above 1080×1920.
- If PyAV transcoding proves too fiddly, add **imageio-ffmpeg** as an optional extra (`uv sync --extra
  transcode`) and run `ffmpeg.exe` from the wheel with `CREATE_NO_WINDOW`.

---

## 4. Public media URLs (Instagram, TikTok `PULL_FROM_URL`)

### 4.1 Who needs a public URL

| Network / content | Needs public URL? | Source |
|---|---|---|
| Facebook photo, video, Reel, Story | **No.** Upload local bytes. | 02 §0, §3 |
| Instagram video (Reel / Story / carousel video) via Facebook Login | **No.** `upload_type=resumable` + rupload. Exception: a custom Reel cover via `cover_url` needs a public JPEG, so use `thumb_offset` instead. | 02 §4.3. Meta's SDK 26.0.2 lists `upload_type`, `cover_url` and `thumb_offset` on `IGUser.create_media` (`adobjects/iguser.py:945-961`). None of the three references uses resumable upload for IG: all send `video_url`. |
| Instagram **images** (feed, carousel, Story image) | **Yes**: `image_url`, JPEG, fetched by Meta | 02 §4.3 |
| Instagram via Instagram Login (no Page) | Yes, for **all** media | 02 §0 |
| TikTok video | **No.** `FILE_UPLOAD` | Brightbean `providers/tiktok.py:322-327`; Postiz `tiktok.provider.ts:713` |
| TikTok **photo** posts | **Yes**, and the domain or URL prefix must be **verified** in the TikTok developer app (`url_ownership_unverified` = 403). `POST /v2/post/publish/content/init/` takes `source=PULL_FROM_URL` only, with 1–35 JPEG or WebP images. | Postiz `tiktok.provider.ts:820`; OpenPost `internal/platform/tiktok.go:26,467-534`; TikTok Content Posting API reference via search |

**Consequence for TikTok photos:** a quick tunnel has a random hostname each run and cannot be verified. TikTok
photo posts need the advanced "own domain" option (a named Cloudflare tunnel or a bucket on a domain the user
verified), or a manual-publish reminder.

### 4.2 How the references tell self-hosters to do it

All three assume a permanently public server:

- **Brightbean:** `APP_URL` plus public `/media/` (`README.md:108,869`), or presigned R2 URLs valid 1 h.
- **Postiz:** `FRONTEND_URL/uploads/...` with random 32-hex names, or R2.
- **OpenPost:** `OPENPOST_MEDIA_URL` with HMAC-signed 15-min URLs, plus a tunnel "for a trial" and ngrok for local
  testing.

None automates a tunnel. Our on-demand tunnel is new design and needs early testing (02 §9.5).

### 4.3 Cloudflare quick tunnel — facts (from cloudflared 2026.10.0 source and docs)

**Licence and binary**

- **Apache-2.0** (`LICENSE`). Downloading the official binary at runtime is fine; redistributing it is also allowed
  with the licence notice.
- Latest tag **2026.10.0** (`git ls-remote`). Recent releases added **"protected" quick tunnels** with Cloudflare
  Access OTP via `--allowed-mail` (`RELEASE_NOTES` 2026.9.2, TUN-10803). Meta's fetcher cannot log in, so we must
  **not** use that mode.
- Windows asset `cloudflared-windows-amd64.exe` (plus `-386`, and an MSI) at
  `https://github.com/cloudflare/cloudflared/releases/download/<tag>/…`. pycloudflared uses
  `releases/latest/download/cloudflared-windows-amd64.exe` (`pycloudflared/download_url.toml`).
- Release notes carry a "### SHA256 Checksums" block (`github_message.py:70`).
- `winget install --id Cloudflare.cloudflared` also exists (Cloudflare downloads page via search).

**How it starts**

- `cloudflared tunnel --url http://127.0.0.1:PORT` runs a quick tunnel whenever `--url` (or `--hello-world`) is set
  and `--quick-service` (default `https://api.trycloudflare.com`) is non-empty
  (`cmd/cloudflared/tunnel/cmd.go:255-260,870-872`).
- It POSTs `{quick-service}/tunnel` with an empty body (public mode) and receives a hostname, an id and a secret
  (`quick_tunnel.go:117-186`).
- It logs: "Your quick Tunnel has been created! … **(it may take some time to be reachable)**"
  (`quick_tunnel.go:97-101`).

**Getting the URL programmatically**

- The metrics server exposes **`GET /quicktunnel` → `{"hostname":"<x>.trycloudflare.com"}`**, plus `/ready` and
  `/healthcheck` (`metrics/metrics.go:79-88`).
- Default metrics ports: `localhost:20241-20245`, or random (`metrics.go:49-51`). Pass `--metrics 127.0.0.1:<port>`
  explicitly.
- Fallback: regex the log line for `https://…trycloudflare.com` (pycloudflared `try_cloudflare.py:14`).

**Terms and limits**

- From the disclaimer in the binary: "**no uptime guarantee**, subject to the Cloudflare Online Services Terms of
  Use … reserves the right to investigate your use … If you intend to use Tunnels in production you should use a
  pre-created named tunnel" (`quick_tunnel.go:26`).
- Docs, confirmed in the `cloudflare/cloudflare-docs` repo (`src/content/docs/tunnel/get-started/quick-tunnels/index.mdx`,
  "Limitations"):
  - no uptime guarantee;
  - 200 in-flight requests per tunnel (beyond that, 429);
  - no SSE;
  - "for testing and development";
  - the hostname changes on every run;
  - email authentication (`--allowed-mail`) "does not support non-interactive clients", which rules it out for
    Meta's fetcher.
- Older docs say quick tunnels fail if `config.yaml` exists in `~/.cloudflared` (UNVERIFIED for 2026). Pass
  `--config <our-empty.yml>` so the user's own config is never picked up. `--config` is a regular flag defaulting
  to the found config path (`cmd.go:909-913`; default search dirs in `config/configuration.go:24-35`).
- Other flags we use: `--no-autoupdate` (`flags/flags.go:126-127`). `--protocol http2` is a fallback when UDP/QUIC
  is blocked (`flags.go:70`).

### 4.4 Automating it: lifecycle and the public-media server

**Components**

- **Public-media app.** A second, minimal Starlette app bound to `127.0.0.1:{PUBPORT}` (for example 8766), served
  by its own `uvicorn.Server` task in the same process. Routes:
  - `GET|HEAD /m/{token}.jpg` (`.mp4` for IG-Login video);
  - `GET /healthz`;
  - anything else → 404. No cookies, no sessions, no directory listing, no redirects.
  - Responses carry `Content-Type` from the rendition, `Content-Length`, `X-Content-Type-Options: nosniff` and
    `Cache-Control: no-store`. Starlette 1.8.0 `FileResponse` supports `Range` (`starlette/responses.py:289-370`).
  - The main UI app is **never** reachable through the tunnel, because the tunnel targets PUBPORT only.
  - **Gotcha (uvicorn 0.54.0, `uvicorn/server.py:332-349`):** each `Server.serve()` run on the main thread
    installs process-wide SIGINT/SIGTERM handlers. It restores the previous handlers and re-raises any captured
    signal when it exits. With a second server in the process, Ctrl+C first reaches whichever server started last.
    Turn off signal capture on the public-media server (override `capture_signals` with a no-op context manager)
    and stop it explicitly from the main app's lifespan.
- **Tokens** (stateful, so they can be revoked and audited). Table `public_media_token(token_sha256 PK,
  rendition_path, expires_at, revoked_at, first_hit_at, hits, last_ua)`.
  - Token = `secrets.token_urlsafe(16)` (128 bit), stored hashed; lookups compare in constant time.
  - **TTL 60 min.** OpenPost uses 15 min; Brightbean warns that short TTLs fail. Revoke when the container reaches
    `FINISHED` or `ERROR`.
  - **Not single-use.** This refines 02 §4.4, which proposed one-shot tokens. Meta's fetcher may issue more than
    one request (HEAD+GET, retries), so a one-shot token risks 2207052 (UNVERIFIED either way; log `hits` and
    `last_ua` to learn).

**Lifecycle** (driven by the prepare stage of the first IG-image target)

1. `ensure_binary()`:
   - If `bin\cloudflared-<pinned>.exe` is missing, ask for consent once ("Se descargará cloudflared de GitHub
     (Apache-2.0)").
   - Download the pinned tag over HTTPS and verify the SHA-256 against **our** pinned table. Do not trust
     `latest`; pycloudflared downloads `latest` without a checksum.
2. Spawn `subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--config", empty_yml, "--metrics",
   f"127.0.0.1:{mport}", "--url", f"http://127.0.0.1:{PUBPORT}"], creationflags=CREATE_NO_WINDOW)` and record
   the PID in `runtime.json`.
   - Put the child in a Windows **Job Object** with `KILL_ON_JOB_CLOSE` (pywin32 312, cp312–cp315 wheels), so
     cloudflared dies if our process crashes.
   - On startup, kill any stale PID recorded from an earlier run.
3. Poll `http://127.0.0.1:{mport}/quicktunnel` until the hostname is non-empty (≤ 30 s), and `/ready` until 200.
4. **Self-test from outside:** GET `https://{host}/healthz` with our own httpx client until 200 (≤ 60 s,
   back-off). This covers "may take some time to be reachable" and fresh-DNS delays. Fall back to
   `--protocol http2` once on failure.
5. Mint tokens, create the IG containers with `image_url=https://{host}/m/{token}.jpg`, and poll `status_code`.
6. When no IG target is in prepare or processing for 5 min: terminate the process (`proc.terminate()`, then
   `kill()` after 5 s) and revoke all tokens.

**Failure UX:** if the tunnel cannot start (corporate firewall, antivirus, Cloudflare outage), mark the target
`failed` with "No se pudo exponer la imagen para Instagram". Offer retry, or the advanced setting "URL pública
base" (own domain, named tunnel, R2/S3 presigned ≥ 1 h).

### 4.5 ngrok as an alternative

- Requires an account and an authtoken. The agent is a proprietary binary; `pyngrok` 8.1.2 (MIT) only downloads and
  wraps it.
- Free tier (ngrok docs via search): 1 GB/month data out, 20 000 requests, 4 000 req/min, 3 endpoints, **one dev
  domain** (stable hostname).
- An **interstitial warning page** is injected in front of HTML browser traffic and bypassed with a
  `ngrok-skip-browser-warning` header or a non-browser User-Agent. Whether Meta's image fetcher could ever get it is
  UNVERIFIED (02 §4.3).
- The stable dev domain is an advantage, but whether TikTok would accept verifying a `*.ngrok-free.app` URL prefix
  is UNVERIFIED.
- **Verdict:** keep it as a manual "advanced" option only (the user pastes their own public base URL). The default
  stays the Cloudflare quick tunnel.

### 4.6 Security considerations for the tunnel

- What becomes public: one port that only maps unguessable tokens (128 bit) to already-approved, about-to-be-public
  renditions, for ≤ 60 min, on a random hostname. The UI, API and OAuth callbacks are never exposed.
- Cloudflare terminates TLS and can see the image bytes. That is acceptable, because the images are published to
  Instagram minutes later. Say so in the consent text.
- Log every public request: time, token prefix, User-Agent, status. Rate-limit the public app (for example 30
  req/min per token) to dampen scanning. Unknown paths return 404 quickly.
- **Validate redirects:** do not follow or allow any. Serve only files under `media\derived\` via a server-side
  path (never a client path), so path traversal is impossible.
- The main app's `TrustedHostMiddleware` rejects `*.trycloudflare.com` hosts anyway (§6), as defence in depth if the
  ports were ever confused.
- The process must not survive our app: Job Object, plus PID cleanup at start.

---

## 5. Secrets (OAuth tokens, app client secrets)

| Option | Pros | Cons |
|---|---|---|
| Fernet key in a local file + encrypted DB columns | Simple and portable; backup or restore just works if the key is exported | The key file sits next to the DB: anyone who copies the folder gets both |
| Every secret in Windows Credential Manager via `keyring` | OS-protected (DPAPI, tied to the Windows user); no key file | Credential blobs are size-limited: **2560 bytes**. Microsoft's `CREDENTIALW` reference says `CredentialBlobSize` "cannot be larger than CRED_MAX_CREDENTIAL_BLOB_SIZE (5*512) bytes" (MicrosoftDocs/sdk-api `ns-wincred-credentialw.md`), and Wine's `wincred.h` agrees. The 512 figure comes from stale headers (mingw-w64 `wincred.h:114`, Free Pascal). `keyring` writes **UTF-16**, so about 1280 characters fit (`keyring/backends/Windows.py:47-62,136-145`). It also clutters the user's credential list, and DB backups lose the tokens. |
| **Hybrid (chosen)** | Only a 44-char master key goes in Credential Manager (far below any limit). Tokens stay in SQLite, encrypted with authenticated encryption. | Needs a fallback when no keyring backend exists |

**Design**

- `keyring` **25.7.0** (MIT). Its Windows dependency is `pywin32-ctypes`; it uses `WinVaultKeyring` with persistence
  `CRED_PERSIST_ENTERPRISE` by default (`Windows.py:30-35,65-92`).
  - Store `service="PlanificadorRedes"`, `username="master-key-v1"`, value = `Fernet.generate_key()`.
- Encryption: `cryptography` **50.0.2** (Apache-2.0 OR BSD-3-Clause).
  - Windows wheels: `cp311-abi3-win_amd64` covers 3.12–3.14, plus `cp314t` and `cp315-abi3.abi3t`.
  - **No `win_arm64` wheel in any release from 46.0.7 to 50.0.2** (PyPI JSON, checked 2026-10-15). On a
    Windows-on-ARM PC, uv would pick a native arm64 CPython and try to build `cryptography` from source (Rust +
    OpenSSL), which fails. Mitigation: on ARM64, `iniciar.bat` sets `UV_PYTHON_ARCH=x86_64` so uv uses an emulated
    x64 interpreter (uv docs, "Transparent x86_64 emulation on aarch64"). Every other compiled dependency here (av,
    Pillow, pillow-heif, pywin32, winrt, pydantic-core) has win_amd64 wheels. Alternative: drop `cryptography`
    and wrap each token directly with DPAPI (`CryptProtectData`).
  - Use `Fernet` / `MultiFernet` (AES-128-CBC + HMAC-SHA256, timestamped).
  - Store values as `"v1:" + token`, so a future key rotation can use `MultiFernet.rotate`. This mirrors OpenPost's
    key-id envelope (`crypto/encrypt.go:16-60`).
  - Brightbean's AES-GCM + HKDF is equally fine; the choice is taste.
- What is encrypted: access and refresh tokens, Meta app secret, TikTok client secret, launch and session secrets.
  - Not encrypted: account ids, expiry timestamps.
  - The settings UI shows `****abcd` (Brightbean `credentials/models.py:104-113`).
- **Fallback** when `keyring.get_keyring()` returns the `fail` or `null` backend, or Credential Manager is
  unavailable (for example under a different logon session):
  - Write the key to `%LOCALAPPDATA%\PlanificadorRedes\secret.key`, wrapped with **DPAPI**
    (`win32crypt.CryptProtectData`, pywin32 312).
  - Warn in Ajustes.
- **Recovery:** offer "Exportar clave de recuperación" (shown once, user stores it). Restoring a backup on another
  PC requires it.
- **Threat model** (state it in the docs):
  - The scheme protects copies of the data folder: backups, cloud sync, another Windows account on the same PC.
  - It does **not** protect against malware running as the same Windows user, which can call the same APIs.
- **Hygiene:**
  - Never log tokens. Redact `access_token=` from URLs before logging (Postiz strips query strings,
    `social.abstract.ts:571-575`).
  - Store truncated response bodies only after redaction.
  - Keep secrets out of the signed session cookie: Starlette's `SessionMiddleware` cookie is signed, not encrypted
    (`starlette/middleware/sessions.py:16-35`).
- **Task Scheduler interplay:** if the app is ever started by Task Scheduler, use "Run only when user is logged on".
  "Do not store password" (S4U) sessions cannot unlock the user's DPAPI or Credential Manager secrets (knowledge,
  not re-verified).

---

## 6. Local web security

1. **Bind**
   - Uvicorn listens on `host="127.0.0.1"` only, never `0.0.0.0`. A loopback bind also avoids the Windows Firewall
     prompt (knowledge, not re-verified).
   - Fixed default port **8765** (configurable), because OAuth redirect URIs must match exactly. Pick **one
     canonical origin**, `http://localhost:8765`, and redirect the other spelling to it.
   - **The UI and every OAuth redirect URI must use that same host.** Cookies are scoped per host, so a cookie
     set on `localhost` is never sent to `127.0.0.1`. A callback landing on the other spelling arrives with no
     session: the launch-token gate rejects it and the session-bound `state` cannot be checked.
     - Report 03 suggests registering the TikTok Desktop redirect as `http://127.0.0.1:<port>/…`. Use
       `http://localhost:<port>/…` instead; the Desktop platform accepts either host (03 §2.3).
     - Meta's dev-mode auto-allow covers `http://localhost` (02 §2.4).
2. **Host-header allow-list** (DNS-rebinding guard). Starlette 1.8.0 `TrustedHostMiddleware(allowed_hosts=
   ["localhost", "127.0.0.1"])` parses the host, drops the port and answers 400 "Invalid host header" otherwise
   (`starlette/middleware/trustedhost.py:41-53`). A rebinding attack keeps the attacker's hostname in `Host`, so it
   fails here.
3. **CSRF**, even on localhost. Any web page can send a simple cross-site form POST to `http://localhost:8765`.
   - Chrome's Local Network Access permission prompt (shipping since Chrome 142 per a third-party guide via search;
     the Chrome 156 removal of the opt-out is UNVERIFIED) helps in Chrome only.
   - For every `POST/PUT/PATCH/DELETE`:
     - **Origin/fetch-metadata check.** Reject if `Origin` is present and not one of our two origins. Reject if
       `Sec-Fetch-Site` is `cross-site` or `same-site`. Allow `same-origin`, and `none` for direct navigation.
     - **Synchronizer token.** A per-session random token, embedded once as `<body hx-headers='{"X-CSRF-Token":
       "…"}'>` (htmx 2 inherits it to all requests) and as a hidden input for non-HTMX forms. Verify in
       middleware.
     - A custom header forces a CORS preflight for any cross-origin `fetch`. We never answer preflights (no CORS
       middleware), so they fail.
   - Note: htmx 4 makes attribute inheritance explicit by default (search: htmx 4 release notes), another reason to
     pin htmx 2 now.
4. **Cookies**
   - `HttpOnly; SameSite=Lax; Path=/`, with an app-specific name.
   - **Corrected: not `SameSite=Strict`.** A Strict cookie is sent only with same-site requests, and Lax adds
     cross-site top-level navigations (RFC 6265bis, "SameSite" attribute section). The OAuth return is exactly
     that: a cross-site redirect from facebook.com or tiktok.com to `/…/callback`. Under Strict it arrives with no
     session cookie, and every connect flow fails.
     - Lax still withholds the cookie from cross-site POSTs. CSRF protection comes from the token and the
       Origin/`Sec-Fetch-Site` checks in point 3 anyway. Rule: **no GET route ever changes state**, except the
       OAuth callbacks, which are protected by `state`.
     - Brightbean (`config/settings/base.py:315`) and OpenPost (`api/handlers/auth.go:2059`) both use Lax, and so
       does Starlette's `SessionMiddleware` by default.
     - If Strict is wanted later, keep OAuth `state` in a server-side table keyed by the state value (single use,
       10-min expiry), and have the callback answer with a same-origin page that continues the flow.
   - Cookies are **not isolated by port**. RFC 6265bis, Security Considerations: "Cookies do not provide isolation
     by port". Any other dev server on `localhost:*` shares the cookie jar and could read non-HttpOnly cookies or
     toss cookies. It also counts as **same-site**, so SameSite does not stop it. That is why point 3 rejects
     `Sec-Fetch-Site: same-site`. Bind the CSRF token server-side to the session, and never trust a cookie alone.
5. **Authentication**
   - Default: a **launch token**, as Jupyter does.
     - The launcher writes a fresh random token to `runtime.json`, which only the user's profile can read, and
       opens `http://localhost:8765/auth/launch?t=…`. The server exchanges it for a session cookie and discards it.
     - Requests without a session get a Spanish page: "Abre la app desde el acceso directo".
     - This stops other local users and random web pages without any friction.
   - Optional **local password** (Ajustes → Seguridad) for shared PCs: hash with `hashlib.scrypt` (stdlib) or
     `argon2-cffi` 25.1.0. Use a 7-day idle session.
6. **Headers**
   - `Content-Security-Policy: default-src 'self'; img-src 'self' data: blob: https:` (remote avatars and thumbnails
     from fbcdn and tiktokcdn); `script-src 'self'`; `style-src 'self' 'unsafe-inline'`;
     `frame-ancestors 'none'`.
   - **Alpine's standard build needs `'unsafe-eval'`** (Brightbean `config/settings/base.py:359-363`). Either accept
     that locally or use **`@alpinejs/csp` 3.17.4** (`dist/cdn.min.js`), which restricts inline expressions to
     simple property and method references.
   - `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`.
7. **No CORS middleware** on the main app. OAuth `state` is signed, bound to the session and single-use. Use PKCE
   wherever the network supports it.
8. **SSRF:** if a feature downloads a remote URL (for example "importar desde URL"), allow only public HTTPS
   addresses and block private, loopback and link-local IPs after DNS resolution (Postiz
   `local.storage.ts:59-66`).

---

## 7. Frontend without a build step — vendored files

All versions and dist-tags come from `npm view` on 2026-10-15. Every tarball was downloaded and its file list
checked.

| Library | Version (npm `latest`) | Released | Files to vendor (browser-ready) | Global / usage | Licence |
|---|---|---|---|---|---|
| FullCalendar (standard bundle `fullcalendar`) | **6.1.21** (v6 line; recommended) | 2026-06-18 | `index.global.min.js` (284 KB; core + interaction + daygrid + timegrid + list + multimonth; CSS injected by JS) + `@fullcalendar/core@6.1.21/locales/es.global.min.js` | `FullCalendar.Calendar` | MIT |
| ↳ FullCalendar v7 (alternative) | **7.1.2** (`latest` tag; 7.0.0 shipped 2026-06-19) | 2026-10-12 | `all/global.js` (869 KB, **not minified**) + `skeleton.css` + `themes/<classic\|monarch\|breezy\|forma\|pulse>/global.js` + `theme.css` + palette CSS + `locales/es/global.js`. Load order: core → theme → locale (they attach via `FullCalendar.Shared`). Bundles a Temporal shim; uses `globalThis.Temporal` if present. npm usage declares the peer `temporal-polyfill ^1.0.1`. | `FullCalendar.Calendar` | MIT |
| Alpine.js | **3.17.4** | 2026-09-21 | `dist/cdn.min.js` (56 KB, auto-starts). CSP variant `@alpinejs/csp@3.17.4/dist/cdn.min.js`. Plugins (e.g. `@alpinejs/focus`, `persist` 3.17.4) ship `dist/cdn.min.js` and must load **before** Alpine. | `window.Alpine` | MIT |
| htmx | **2.0.11** (`latest`; **4.0.0** under `next`, released 2026-08-28, breaking) | 2026-09-22 | `dist/htmx.min.js` (52 KB) | `window.htmx` | 0BSD |
| Chart.js | **4.5.1** | 2025-10-13 | `dist/chart.umd.min.js` (208 KB) | `window.Chart` | MIT |
| emoji-picker-element | **1.29.1** | 2026-03-01 | ESM only: `index.js` → `picker.js`, `database.js` (relative imports work without a bundler via `<script type="module">`) + `i18n/es.js` | `<emoji-picker>` web component; IndexedDB cache | Apache-2.0 |
| emoji-picker-element-data | **1.8.0** | — | `es/cldr/data.json` (421 KB) or `es/cldr-native/…`. **Set `dataSource`** to our local copy; the default is `cdn.jsdelivr.net/npm/emoji-picker-element-data@^1/en/emojibase/data.json` (seen in `database.js`). | — | Apache-2.0 (data from Emojibase / CLDR) |
| (optional) SortableJS for reordering media | 1.15.7 | 2026-02-11 | `Sortable.min.js` | `window.Sortable` | MIT |

**Recommendations**

- **FullCalendar: 6.1.21 for the MVP.**
  - Smallest single-file path, CSS auto-injected, a well-known API, still patched (6.1.21 shipped 2026-06-18).
  - v7 brings themes and renamed display hooks (official "V7 JavaScript API Migration Guide", via search) and has
    only been out four months.
  - Revisit v7.1.x after the MVP. Its global build works without npm.
  - Both are MIT. The premium plugins (resource timeline etc.) are not needed.
- **htmx: 2.0.11.** 4.0 changes the defaults: fetch-based requests, explicit inheritance, error responses swapped,
  renamed events, a 60-s default timeout (official announcement, via search). Migrate later with the
  `htmx-2-compat` extension if needed.
- **Emoji:**
  - emoji-mart 5.6.0 (2024-04, MIT; `dist/browser.js` global) is stale, and it fetches data from jsdelivr unless
    the data is passed in. Prefer emoji-picker-element.
  - Zero-dependency fallback: Windows' own emoji panel (**Win + .**) works in any textarea. Mention it in the
    composer.
- **Layout:** `static/vendor/<lib>/<version>/…` plus a `static/vendor/LICENSES/` folder with each LICENSE file. Add
  a tiny `scripts/vendor.py` that downloads exact tarball URLs, checks npm `dist.integrity` (sha512) and extracts
  only the listed files. The npm CLI is not needed at runtime or by the user.

---

## 8. Packaging and run experience on Windows

### 8.1 Install and first run

- **Prerequisite: uv.**
  - Install with `winget install --id=astral-sh.uv -e` or the official PowerShell installer.
  - Latest uv on PyPI: **0.13.1** (2026-10-14).
  - uv downloads a managed CPython, so the user never installs Python. Use `requires-python = ">=3.12"` and pin
    `.python-version` to `3.13`.
  - The wheels the app actually loads on Windows were checked for cp312–cp314 above: av, Pillow, pillow-heif,
    cryptography, pywin32, winrt-*, time-machine, playwright. watchfiles 1.3.0 only shows cp310 and cp314 tags
    plus likely abi3 (dev-only, irrelevant).
- `pyproject.toml`: `[project.scripts] planificador = "planificador.cli:main"`.
- `iniciar.bat` (double-click), in our own words:

```bat
@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul || (echo Falta "uv". Instálalo con:  winget install --id=astral-sh.uv -e & pause & exit /b 1)
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set UV_PYTHON_ARCH=x86_64
uv sync --frozen --no-dev || (pause & exit /b 1)
uv run --frozen --no-dev planificador %*
```

- What `planificador` (cli.main) does:
  1. Takes the single-instance lock (`filelock`). If the lock is held, it reads `runtime.json`, opens the browser
     on the running instance and exits.
  2. Creates the data dirs and runs DB migrations, then the keyring/key check.
  3. Starts `uvicorn.Server(Config(app, host="127.0.0.1", port=8765, loop="asyncio"))` and waits for `/healthz`.
  4. Calls `webbrowser.open("http://localhost:8765/auth/launch?t=…")` (skipped with `--headless`).
- Console window: keep it visible by default ("no cierres esta ventana"; Ctrl+C stops cleanly). With
  `--minimized`, the `.bat` relaunches itself via `start "" /min`.
  - A hidden-console variant can run `.venv\Scripts\pythonw.exe -m planificador --headless`. Verified: uv's venv
    creation copies `pythonw.exe` on Windows (astral-sh/uv `crates/uv-virtualenv/src/virtualenv.rs:305,428`).
- **Windows on ARM:** detect `PROCESSOR_ARCHITECTURE=ARM64` in `iniciar.bat` and `set UV_PYTHON_ARCH=x86_64`
  before `uv sync`, because `cryptography` has no win_arm64 wheel (§5).
  - Optional tray icon: `pystray` 0.19.5 (LGPL-3.0, 2023) with Abrir / Pausar publicaciones / Salir.
- Set UTF-8 for logs (`PYTHONUTF8=1` in the `.bat`, or `sys.stdout.reconfigure(encoding="utf-8")`) for Spanish
  accents. Write rotating log files to `logs\`.

### 8.2 Start with Windows (so scheduled posts go out whenever the PC is on)

- **Default: Startup-folder shortcut**, no admin. Path: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
  Planificador de Redes.lnk` → `iniciar.bat --headless --minimized`. The path is `FOLDERID_Startup` (per user) in
  Microsoft's KNOWNFOLDERID reference (MicrosoftDocs/win32 `desktop-src/shell/knownfolderid.md`). Resolve it with
  `SHGetKnownFolderPath`, not a hard-coded string.
  - The Ajustes toggle "Iniciar con Windows" creates or deletes it (via the `WScript.Shell` COM object through
    pywin32, or a `.url`/`.bat` file).
  - Equivalent: the `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value.
- **Advanced: Task Scheduler** (`schtasks /Create … /SC ONLOGON` or an XML import).
  - Settings: "Run only when user is logged on" (keyring access, §5); "Run task as soon as possible after a
    scheduled start is missed"; optional `WakeToRun`.
  - Creating ONLOGON tasks may need elevation (UNVERIFIED). Modern Standby machines may not honour wake timers
    (Microsoft Q&A reports via search).

### 8.3 Notifications (manual-publish reminders, failures, "missed" posts)

| Option | Works when | Windows support | Verdict |
|---|---|---|---|
| Browser Notification API from the open tab | Only while a tab is open. Background tabs' timers are throttled (fine at minute granularity). Needs a permission click. Secure context: `http://localhost` counts as potentially trustworthy under the W3C Secure Contexts spec (`w3c/webappsec-secure-contexts` `index.bs`, the "localhost" rules), as does `127.0.0.0/8`. | Any browser | **Secondary.** Also show in-page toasts and a badge. |
| **desktop-notifier 6.2.0** (MIT, 2025-08-08, pure wheel) | Whenever the Python process runs, even without a browser | Windows backend `WinRTDesktopNotifier` on `winrt-*` ≥3.1. **winrt-runtime / winrt-Windows.UI.Notifications 3.2.1** ship cp312, cp313 and cp314 wheels for win32, amd64 and arm64. Clickable callbacks and buttons where supported. It registers an AppUserModelID under HKCU. Older docs (3.5) labelled Windows "experimental"; current docs do not. Async API fits asyncio. | **Primary.** Click → open `http://localhost:8765/posts/{id}`. |
| windows-toasts 1.3.1 / win11toast 0.36.3 | Same | Windows-only | Not needed |

**Manual-publish reminder flow** (for content the APIs cannot publish, e.g. IG Stories with stickers or music, or
TikTok when the app is unaudited):

1. At T: toast "Es hora de publicar en TikTok: <título>".
2. The post page offers "Copiar texto" and "Descargar archivo".
3. Mark the target as "Publicado manualmente".

### 8.4 What happens if the PC is off or asleep at publish time

- Nothing publishes until the app runs again. On start or wake, the catch-up policy applies (§2.5): auto-publish
  within the grace window, otherwise `missed` plus a notification.
- Facebook-only mitigation: native scheduling hand-off (02 §3.7).
- **Product copy must be explicit:** "Las publicaciones solo salen si el PC está encendido y la app abierta. Si no,
  te avisaremos al volver."

---

## 9. Testing strategy

- **Runner:** pytest **9.1.1**. Async tests use **pytest-asyncio 1.4.0**, or AnyIO's pytest plugin (anyio 4.15.1
  ships with Starlette). FastAPI tests use `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))`.
- **HTTP mocking: respx 0.23.1** (BSD-3-Clause, `Requires-Dist: httpx>=0.25.0`; httpx latest is 0.28.1).
  - Run `respx.mock(assert_all_mocked=True, assert_all_called=False)` globally, so any unmocked real request fails
    the test.
  - One fixture module per provider holds recorded-shape JSON (written by us from docs and 02 and 03, never copied
    from the references).
  - **Must-have scenarios:**
    - Happy path per network and content type.
    - Rate limit with a reset time → `next_attempt_at` equals the reset.
    - Token expired → refresh → retry pre-commit.
    - **Timeout on the commit call** → no second commit call is made (assert the route's `call_count == 1`) → the
      reconcile route is queried → state becomes `published` or `needs_review`.
    - IG container `ERROR` / `EXPIRED`.
    - TikTok `PROCESSING_UPLOAD` → `PUBLISH_COMPLETE`.
    - A 400 on a comment that nevertheless created it (Brightbean's lesson): no retry.
- **Time:**
  - Inject a `Clock` (`now_utc()`) into the engine so most tests need no patching. Tick the engine manually:
    `await engine.tick(now=...)`.
  - Integration tests that cross library code use **time-machine 3.5.1**: MIT, a C extension with Windows wheels
    cp310–cp315 for amd64 and arm64. It does **not** mock `time.monotonic()`, so asyncio keeps working (comment in
    `time_machine/cli.py:1078`).
  - freezegun 1.5.5 also works but patches monotonic and perf_counter. Pass `real_asyncio=True`, or
    `asyncio.sleep` misbehaves (`freezegun/api.py:600,834-844`).
  - Scenarios:
    - DST transitions in Europe/Madrid.
    - Grace-window boundaries.
    - Wake detection: a 3-hour jump between ticks.
    - Crash recovery: start the engine with rows in each `stage` and assert retry vs reconcile.
    - Concurrent double tick: two `tick()` coroutines on the same due row → exactly one claim.
- **Media:** generate fixtures in tests (Pillow images with EXIF orientation, CMYK, alpha, 50 MP bomb; a 2-s
  H.264/AAC MP4, and a rotated and an HEVC variant encoded with PyAV). This keeps binary blobs out of git. Assert
  the probe results, the poster, and the rendition sizes and aspect ratios.
- **Tunnel:**
  - Unit-test the token store and the public app (404 for unknown or expired tokens, Range, HEAD).
  - Fake `cloudflared` with a tiny script that serves `/quicktunnel`.
  - An opt-in manual test (`-m live_tunnel`) starts the real binary.
- **Playwright smoke tests:** pytest-playwright **0.10.0** + playwright **1.64.0** (wheels for win32, win_amd64 and
  win_arm64; `uv run playwright install chromium`).
  - Fixture: start uvicorn in a background thread on a free port, with a temp data dir and
    `PLANIFICADOR_FAKE_PROVIDERS=1`, a fake provider layer that records calls in memory.
  - Smoke paths:
    - Launch-token login.
    - Calendar renders in Spanish.
    - Create a post in the composer with an image → appears in the calendar → drag to another day (HTMX update)
      → "Publicar ahora" → status "Publicado".
    - A CSRF-less POST gets 403.
    - The Ajustes page masks secrets.
- **CI:** GitHub Actions `windows-latest` (paths, file locking, tzdata, keyring fallback) plus `ubuntu-latest`. Lint
  with ruff.

---

## 10. Open questions and risks

1. **Default grace window** for late posts (2 h proposed). Should it differ per network, or per post type (Stories
   vs feed)? Product decision.
2. **Instagram fetcher vs quick tunnel:** reachability of fresh `*.trycloudflare.com` hosts, how many requests Meta
   makes per image (decides single-use vs multi-use tokens), and the minimum safe TTL (Brightbean vs OpenPost
   disagree). Test in week 1 with the real Meta app. (Same as 02 §9.5.)
3. **Cloudflare quick-tunnel ToS** say testing only, with no uptime guarantee. Low-volume personal use is probably
   tolerated but not promised. Keep the "URL pública base" escape hatch.
4. **TikTok photo posts** need a verified domain or URL prefix, so they cannot work with quick tunnels. Do we
   support them only with the user's own domain, or only as manual reminders?
5. **Credential Manager blob limit:** resolved, 2560 bytes per Microsoft's docs. It does not affect the hybrid
   design. Keyring under Task Scheduler or other logon sessions still needs a test.
6. **PyAV specifics:** the rotation API is `VideoFrame.rotation` (its sign is still to be tested). Still open: GIL
   behaviour during decode, wheel size (≈28 MB), and the licensing of the bundled x264/x265 DLLs if we ever ship an
   installer.
7. **Windows sleep semantics:** the effect of `QueryPerformanceCounter`-based `time.monotonic` across suspend
   (Python ≥3.13), wake timers on Modern Standby, and whether `SetThreadExecutionState` is enough for long uploads
   on battery.
8. **Python/SQLite versions:** mostly resolved. CPython's Windows externals are SQLite 3.49.1 (3.12) and 3.50.4
   (3.13/3.14), and python-build-standalone pins 3.53.1, all far above the 3.35 that `RETURNING` needs. Keep the
   startup assertion.
9. **FullCalendar v6 vs v7:** v6 is in maintenance. Decide when to migrate and test v7's global build with our
   Alpine and HTMX partial swaps.
10. **htmx 4** is out (2026-08-28) but not `latest` on npm. Pin 2.0.11 and track the end-of-support of 2.x
    (UNVERIFIED claims of "indefinite" support).
11. **ONLOGON task creation** may require admin. The Startup folder is the safe default; verify on Windows 11
    23H2/24H2 without admin rights.
12. **Antivirus or SmartScreen** may flag a freshly downloaded `cloudflared.exe` or block its outbound QUIC. Plan a
    clear error message and the `--protocol http2` fallback.
13. **Port 8765 conflicts** can break the OAuth redirect URIs registered in the user's Meta and TikTok apps.
    - A TikTok Desktop redirect may use a wildcard port (`http://localhost:*/…`, 03 §2.3).
    - Meta auto-allows `http://localhost` redirects in development mode (02 §2.4).
    - So a port change may need no re-registration, but that is UNVERIFIED for Meta apps in Live mode. The UI must
      still explain it.
14. **Web-search budget:** it ran out again during the verification pass. Re-checked since then by other means:
    cookie port-sharing (RFC 6265bis), the Startup-folder path (KNOWNFOLDERID), `SetThreadExecutionState` scope
    (MicrosoftDocs), and localhost as a secure context (W3C Secure Contexts). Still not re-verified: SQLite
    `BEGIN IMMEDIATE` and `SQLITE_BUSY_SNAPSHOT` semantics, and the Windows Firewall prompt on a loopback bind.
15. **Windows on ARM:** `cryptography` has no win_arm64 wheel. The x64-emulation workaround (`UV_PYTHON_ARCH`) must
    be tested on a Snapdragon PC, or the token encryption must switch to DPAPI only.
16. **SameSite and the OAuth return** (fixed in §6.4): test the full Meta and TikTok connect flows in Chrome, Edge
    and Firefox with the Lax cookie.

---

## 11. Sources

**Reference repositories** (read only, AGPL)

- brightbean-studio:
  - `apps/publisher/{engine.py,tasks.py,apps.py,models.py,management/commands/run_publisher.py}`
  - `apps/common/{worker.py,db.py,encryption.py,background.py}`
  - `apps/media_library/{models.py,validators.py,services.py,tasks.py,storage.py}`
  - `apps/credentials/models.py`, `apps/social_accounts/models.py`
  - `providers/{base.py,types.py,exceptions.py,tiktok.py,instagram.py,facebook.py}`
  - `config/settings/{base.py,development.py}`, `README.md`, `requirements.txt`
  - `apps/social_accounts/management/commands/instagram_review_test_calls.py`
- postiz-app:
  - `apps/orchestrator/src/workflows/post-workflows/post.workflow.v1.1.2.ts`
  - `apps/orchestrator/src/workflows/missing.post.workflow.ts`, `apps/orchestrator/src/activities/post.activity.ts`
  - `libraries/nestjs-libraries/src/database/prisma/posts/posts.repository.ts`
  - `libraries/nestjs-libraries/src/temporal/temporal.module.ts`
  - `libraries/nestjs-libraries/src/integrations/{social.abstract.ts,social/*.provider.ts}`
  - `libraries/nestjs-libraries/src/upload/local.storage.ts`
  - `libraries/helpers/src/auth/auth.service.ts`, `schema.prisma`, `docker-compose.yaml`
- openpost:
  - `apps/server/internal/database/database.go`, `internal/queue/worker.go`
  - `internal/services/publisher/{failures.go,publisher.go}`
  - `internal/services/providerwrite/types.go`, `internal/platform/adapter.go`
  - `internal/services/{mediasigner/signer.go,publicurl/media.go,crypto/encrypt.go}`
  - `internal/api/handlers/media.go`, `internal/config/config.go`
  - `docs/reference/providers/{threads.md,troubleshooting.md}`
  - `apps/docs/content/docs/self-hosting/reverse-proxy.mdx`

**Other code inspected**

- cloudflared 2026.10.0 (`git clone --depth 1 --branch 2026.10.0 https://github.com/cloudflare/cloudflared`):
  - `cmd/cloudflared/tunnel/{quick_tunnel.go,cmd.go}`
  - `metrics/metrics.go`
  - `cmd/cloudflared/flags/flags.go`
  - `config/configuration.go`
  - `RELEASE_NOTES`, `github_message.py`, `LICENSE`
- Wheels and sdists from PyPI:
  - av 19.0.1, imageio-ffmpeg 0.6.0, pymediainfo 7.0.1 (file lists, FFmpeg configure string)
  - keyring 25.7.0 (`keyring/backends/Windows.py`)
  - freezegun 1.5.5, time-machine 3.5.1, respx 0.23.1
  - uvicorn 0.54.0, starlette 1.8.0
  - pycloudflared 0.3.0

**Package registries**

- PyPI JSON (`https://pypi.org/pypi/<pkg>/json`), checked 2026-10-15. Packages: fastapi 0.143.2, uvicorn 0.54.0,
  starlette 1.8.0, httpx 0.28.1, jinja2 3.1.6, python-multipart 0.0.32, sqlalchemy 2.1.4, aiosqlite 0.22.1,
  alembic 1.20.0, pillow 12.3.0, pillow-heif 1.8.1, av 19.0.1, imageio-ffmpeg 0.6.0, pymediainfo 7.0.1,
  cryptography 50.0.2, keyring 25.7.0, pywin32 312, desktop-notifier 6.2.0, winrt-runtime 3.2.1, apscheduler
  3.11.3 (4.0.0a6), aiolimiter 1.3.0, tenacity 9.2.1, platformdirs 4.13.0, filelock 4.2.0, tzdata 2026.5,
  tzlocal 5.4.4, pycloudflared 0.3.0, pyngrok 8.1.2, pystray 0.19.5, argon2-cffi 25.1.0, respx 0.23.1,
  freezegun 1.5.5, time-machine 3.5.1, pytest 9.1.1, pytest-asyncio 1.4.0, anyio 4.15.1, playwright 1.64.0,
  pytest-playwright 0.10.0, uv 0.13.1.
- npm (`npm view <pkg> version dist.tarball`, tarballs inspected): fullcalendar 6.1.21 / 7.1.2,
  @fullcalendar/core 6.1.21, alpinejs 3.17.4, @alpinejs/csp 3.17.4, htmx.org 2.0.11 / 4.0.0, chart.js 4.5.1,
  emoji-picker-element 1.29.1, emoji-picker-element-data 1.8.0, emoji-mart 5.6.0, sortablejs 1.15.7,
  temporal-polyfill 1.0.6.

**Web** (via search-result snippets)

- Cloudflare Quick Tunnels: <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/>,
  <https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/>
- Cloudflare downloads (winget id): <https://developers.cloudflare.com/argo-tunnel/downloads>
- ngrok free-plan limits and interstitial: <https://ngrok.com/docs/pricing-limits/free-plan-limits>,
  <https://ngrok.com/docs/errors/err_ngrok_6024.md>
- TikTok Content Posting API (PULL_FROM_URL verification, `url_ownership_unverified`, pending-share cap):
  <https://developers.tiktok.com/doc/content-posting-api-reference-upload-video>,
  <https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide>
- IG Container (`EXPIRED` after 24 h): <https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-container>;
  IG error codes: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/error-codes>
- desktop-notifier docs: <https://desktop-notifier.readthedocs.io/en/latest/>
- MDN Notifications API: <https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API>
- Credential blob size: <https://docs.freepascal.org/daily/packages/winunits-jedi/jwawincred/cred_max_credential_blob_size.html>,
  <https://lists.cyberduck.io/pipermail/cyberduck-trac/2021-June/046049.html>
- Modern Standby and wake timers: <https://learn.microsoft.com/en-us/answers/questions/1660115/regarding-waking-up-from-sleep-on-a-modern-standby>,
  <https://learn.microsoft.com/en-us/answers/questions/5657944/scheduled-task-not-waking-computer>
- CPython `time.monotonic` → QPC (gh-88494): <https://git.joshthomas.dev/mirrors/cpython/commit/846ad5a26ac0ff988a3fceec8f8e830f68bdf48a>; PEP 418 <https://peps.python.org/pep-0418/>
- Chrome Local Network Access (third-party): <https://www.superchargebrowser.com/library/chrome-156-local-network-access.md>
- htmx 4 release: <https://four.htmx.org/announcements/2026-08-28-htmx-4.0.0-is-released>, <https://www.infoq.com/news/2026/09/htmx-4-released/>
- FullCalendar v7 migration: <https://fullcalendar.io/docs/upgrading-from-v6>, <https://fullcalendar.io/docs/upgrading-from-v6-js>

---

## 12. Verificación (adversarial fact-check, 2026-10-15)

**Method.** Each claim was re-checked against a source other than the one the report first cited:

- registry JSON from pypi.org and registry.npmjs.org, and package tarballs or wheels downloaded and opened;
- upstream source and docs read through raw.githubusercontent.com: cloudflared, cloudflare-docs, MicrosoftDocs
  sdk-api/win32, CPython, astral-sh/uv, python-build-standalone, Meta's `facebook-python-business-sdk`, keyring,
  mingw-w64, Wine, httpwg RFC 6265bis, W3C Secure Contexts;
- whichever of the three reference repos the report had not cited for that claim.

The web-search budget was already exhausted, so no new search results were used.

| # | Claim | Verdict | Source used for the check |
|---|---|---|---|
| 1 | PyPI versions and Windows wheels: av 19.0.1 (`cp312-abi3` win_amd64/arm64), Pillow 12.3.0, pillow-heif 1.8.1, keyring 25.7.0, time-machine 3.5.1, respx 0.23.1, APScheduler 3.11.3 / 4.0.0a6, tzdata 2026.5, uv 0.13.1, playwright 1.64.0, pytest-playwright 0.10.0, desktop-notifier 6.2.0 (winrt ≥3.1), winrt-runtime 3.2.1 (cp312–cp314 win32/amd64/arm64) | CONFIRMED | PyPI JSON, re-queried |
| 2 | `cryptography` 50.0.2 wheels "cover Windows" | **CORRECTED**: win_amd64 only. No win_arm64 wheel in 46.0.7 through 50.0.2. Added the `UV_PYTHON_ARCH=x86_64` workaround (§5, §8.1) | PyPI JSON; astral-sh/uv `docs/concepts/python-versions.md` (x86_64 emulation) |
| 3 | npm versions and dist-tags: fullcalendar `latest`=7.1.2 (6.1.21 from 2026-06-18), alpinejs and @alpinejs/csp 3.17.4, htmx.org `latest`=2.0.11 and `next`=4.0.0, chart.js 4.5.1, emoji-picker-element 1.29.1 and its data package 1.8.0. All listed vendored files exist in the tarballs, including `@fullcalendar/core/locales/es.global.min.js` and `es/cldr/data.json`, and the default jsdelivr `dataSource` | CONFIRMED | npm registry and tarballs |
| 4 | htmx 4 changes: 60 s default timeout, explicit inheritance, `htmx-2-compat` extension | CONFIRMED | `htmx.org@4.0.0` `dist/htmx.js:196,203` and `dist/ext/htmx-2-compat.js` (the report had cited the release blog) |
| 5 | cloudflared 2026.10.0 is the latest tag. It exposes `/quicktunnel`, `/ready` and `/healthcheck` with default metrics ports 20241–20245, and carries the "no uptime guarantee" disclaimer and the "may take some time to be reachable" message | CONFIRMED | `git ls-remote`; raw `metrics/metrics.go:49-51,80-88`; `quick_tunnel.go:26,99,145` |
| 6 | Quick-tunnel limits: 200 in-flight requests then 429, no SSE, testing only | CONFIRMED (adds: the hostname changes on every run, and `--allowed-mail` does not support non-interactive clients) | cloudflare-docs repo, `tunnel/get-started/quick-tunnels/index.mdx` "Limitations" |
| 7 | Credential Manager blob limit "512 or 2560, sources disagree" | **CORRECTED**: 2560 bytes (5×512) per Microsoft's `CREDENTIALW` reference; 512 is a stale mingw-w64 header value | MicrosoftDocs/sdk-api `ns-wincred-credentialw.md`; Wine `wincred.h`; mingw-w64 `wincred.h:114` |
| 8 | keyring writes UTF-16, defaults to `CRED_PERSIST_ENTERPRISE`, and depends on `pywin32-ctypes` | CONFIRMED | keyring v25.7.0 `keyring/backends/Windows.py` (GitHub); PyPI `requires_dist` |
| 9 | Bundled SQLite is new enough for `RETURNING` (≥3.35) | CONFIRMED (was open) | CPython `PCbuild/get_externals.bat` (3.12: 3.49.1; 3.13/3.14: 3.50.4); python-build-standalone `downloads.json` (3.53.1) |
| 10 | Uvicorn 0.54.0 uses `ProactorEventLoop` on Windows only without reload/workers; `TrustedHostMiddleware` returns 400 "Invalid host header"; `FileResponse` supports Range | CONFIRMED (adds: per-`Server` signal capture, a gotcha for the second server, §4.4) | uvicorn and starlette 1.8.0 wheels |
| 11 | Session cookie `SameSite=Strict` | **CORRECTED → `Lax`**. Strict cookies are not sent on the cross-site redirect back from Meta or TikTok, so every OAuth callback would fail (§6.4) | RFC 6265bis (httpwg draft, SameSite and Top-level Navigations); Brightbean `base.py:315`; OpenPost `auth.go:2059` |
| 12 | Canonical OAuth origin | **CORRECTED**: the UI and all redirect URIs must share the host `localhost`. Report 03's `127.0.0.1` TikTok redirect would lose the session cookie (§6.1) | Cookie host scoping (RFC 6265bis); 03 §2.3 (the Desktop platform accepts either host) |
| 13 | Cookies are not isolated by port; `http://localhost` is a secure context | CONFIRMED | RFC 6265bis Security Considerations; W3C `webappsec-secure-contexts/index.bs` |
| 14 | `SetThreadExecutionState` prevents idle sleep only, not lid or power-button sleep | CONFIRMED (adds: the flags are per thread) | MicrosoftDocs/sdk-api `nf-winbase-setthreadexecutionstate.md` |
| 15 | Windows has no IANA tz database (`tzdata` required); Python 3.13 `time.monotonic` uses QPC on Windows | CONFIRMED (whether QPC counts time spent suspended is still UNVERIFIED; the design does not depend on it) | CPython `Doc/library/zoneinfo.rst:95-99`, `Doc/library/time.rst` (3.13) |
| 16 | time-machine does not mock `time.monotonic`; freezegun needs `real_asyncio=True` | CONFIRMED | time-machine 3.5.1 C-extension symbol table (no monotonic or perf_counter); freezegun `api.py:600,834` |
| 17 | PyAV rotation API ("UNVERIFIED") and its bundled DLLs | **RESOLVED**: `VideoFrame.rotation` (int). The wheel ships libx264, libx265, libvpx, SvtAv1Enc and dav1d DLLs | av 19.0.1 win_amd64 wheel (`av/video/frame.pyi:69-70`, `av.libs/`) |
| 18 | uv venvs include `pythonw.exe` ("UNVERIFIED"); the winget id is `astral-sh.uv`; the Startup-folder path | CONFIRMED / RESOLVED | astral-sh/uv `virtualenv.rs:305,428` and `docs/getting-started/installation.md:112`; MicrosoftDocs/win32 `knownfolderid.md` |
| 19 | Who needs a public URL: IG images yes; IG video via Facebook Login no (resumable); FB never; TikTok video no (`FILE_UPLOAD`); TikTok photos `PULL_FROM_URL` from a verified domain only | CONFIRMED with caveats: a Reel `cover_url` needs a public URL (use `thumb_offset`); no reference uses IG resumable upload, so it is proven only by 02's ig-publish; photos are JPEG/WebP, 1–35 images | Meta SDK v26.0.2 `iguser.py:945-961`; OpenPost `tiktok.go:26,467-534`; Brightbean `tiktok.py:323-324` |
| 20 | IG container `status_code` values (FINISHED, PUBLISHED, ERROR, EXPIRED) and "PUBLISHED = done" for reconcile; TikTok `POST /v2/post/publish/status/fetch/` with `publish_id`; FB video `status.publishing_phase`; FB native scheduling `published=false` + `scheduled_publish_time` | CONFIRMED (the 24-h container expiry rests on the Meta reference via 02 only) | OpenPost `instagram.go:683-685,1125-1139`, `tiktok.go:27`, `facebook.go:512-515,925`; Brightbean `tiktok.py:86-96,560-571`; Meta SDK `page.py:2308,2313` |
| 21 | TikTok rate-limit codes: "`spam_risk_too_many_pending_share` = 5/24 h" listed as the generic limit | **CORRECTED**: that cap is for inbox (draft) shares and becomes `awaiting_user`. The Direct Post daily cap is `spam_risk_too_many_posts` (§2.4) | Postiz `tiktok.provider.ts:187-200`; 03 (error table) |
| 22 | Reference-repo facts: Brightbean 15 s / 60 s cycles, `[60,300,1800]`, 3 retries, 6 h unconfirmed, the "short-TTL signed URLs will not work" note, `tzdata` pin; Postiz hourly sweep over 2 days; OpenPost WAL / `busy_timeout=5000` / `synchronous=NORMAL` / one connection, 15-min URLs | CONFIRMED (re-read) | Cited files |
| 23 | IG image spec: aspect 4:5 to 1.91:1, FB photo ≤ 4 MB, IG "JPEG only" | Aspect and 4 MB CONFIRMED (Postiz `instagram.provider.ts:301`, `facebook.provider.ts:111`). JPEG-only is disputed by the references (Brightbean's review command says "JPEG/PNG", OpenPost sends PNG/WebP), so converting to JPEG stays the safe default. The 8 MB limit is UNVERIFIED | Postiz, Brightbean `instagram_review_test_calls.py:47`, OpenPost `instagram.go:1285-1293` |
| 24 | `BEGIN IMMEDIATE` avoids `SQLITE_BUSY_SNAPSHOT` in WAL; a loopback bind avoids the firewall prompt | UNVERIFIED (no primary source reachable; low risk because writes are serialized in-process) | — |

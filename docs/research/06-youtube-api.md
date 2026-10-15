# 06 — YouTube (Google OAuth + YouTube Data API v3 + YouTube Analytics API v2) for a LOCAL app

Research date: **2026-10-15**. Target: "Planificador de Redes", a single-user web app on the user's Windows PC at
`http://localhost:8765` (Python 3.12, FastAPI, sync `httpx`, SQLite). The user brings their **own** Google Cloud
project and OAuth client. Goal: connect a YouTube channel, publish regular videos and Shorts (now or scheduled),
read channel/video analytics and read/answer comments. Context from the user: the **Conexiones** page should only
offer the networks we support (Facebook, Instagram, TikTok) **plus YouTube**.

How to read this document

- **Ground truth for parameter names** = Google's machine-readable discovery documents bundled in
  `google-api-python-client` **2.201.0** (PyPI upload 2026-09-30): `youtube.v3.json` (revision **20260924**) and
  `youtubeAnalytics.v2.json` (revision 20240707). Older wheels 2.187.0 (rev 20250714) and 2.200.0 (rev 20260820)
  were diffed to date changes. Also read: `googleapiclient/http.py` (2.201.0, upload/retry logic),
  `google-auth-oauthlib` 1.5.0 `flow.py` (Google's own installed-app OAuth helper), the official sample
  `youtube/api-samples/python/upload_video.py`.
- **Reference implementations** (AGPL, read only, nothing copied; we only describe behaviour):
  - `postiz-app` (commit `021c2ee`, 2026-10-15): `libraries/nestjs-libraries/src/integrations/social/youtube.provider.ts`,
    `libraries/nestjs-libraries/src/dtos/posts/providers-settings/youtube.settings.dto.ts`.
  - `brightbean-studio` (commit `dbe065f`, 2026-10-14): `providers/youtube.py`, `providers/google_errors.py`,
    `apps/common/quota.py`, `development_specs/youtube-audit-remediation.md` (a real YouTube compliance-review
    response, Oct 2026), `apps/common/validators.py`, `apps/composer/views.py`.
  - `openpost` (commit `dab6e02`, 2026-10-15): `apps/server/internal/platform/youtube.go`, `analytics_video.go`,
    `postimport_youtube.go`, `first_comment_youtube.go`, `google_oauth.go`, `providerlimits/youtube.go`,
    `capabilities/capabilities.go`, docs `docs/reference/providers/youtube.md`,
    `docs/research/provider-production-review-requirements-2026-09-19.md`.
  - Also: `porjo/youtubeuploader` README (raw.githubusercontent.com), `rclone` `docs/content/drive.md` (current
    Google Cloud console steps for personal OAuth clients), and an npm-published Google OAuth runbook with an
    **empirical** token-lifetime measurement (`@rubytech/create-realagent-code@0.1.511`,
    `payload/platform/plugins/google/references/auth.md`).
- Official Google pages (developers.google.com, support.google.com) **cannot be fetched** from this container; they
  were read through web-search snippets. Pages used are listed in §16.
- **UNVERIFIED** = no primary source confirmed it, or sources disagree. Test these first with a real project.
- **Fact-check pass (2026-10-15)**: 20 load-bearing claims were re-checked against *different* sources (discovery rev
  **20261012** from `google-api-go-client` main, npm `@googleapis/youtube` 39.0.1/40.0.0/40.0.1, a scraped copy of the
  Data API revision history fetched 2026-09-08, other OSS repos, web search). Results and corrections are in **§17
  Verificación**; the most important one is that the private-lock rule is now **contradicted** by Google's own
  reference text (§3.1).

---

## 0. Summary and decisions

| Topic | Fact (short) | Decision for Planificador de Redes |
|---|---|---|
| **Audit / private lock** | 2020-07-28 rule: "All videos uploaded via the videos.insert endpoint from unverified API projects created after 28 July 2020 will be restricted to private viewing mode. To lift this restriction, each project must undergo an audit…". Locked videos: no appeal, re-upload needed (YouTube Help). **CONFLICT (Oct 2026):** the `videos`/`videos.insert` reference pages (last updated 2026-10-08) and discovery rev 20261012 now say "Videos uploaded from unverified API projects are **not restricted** to private viewing mode", while the 2020 revision-history entry was never retracted and the Help page still describes the lock. **Current enforcement UNVERIFIED** — test it. | The user's own project (created 2026) **is unaudited** unless they pass the YouTube API Services audit. Safe default = **"Publicación asistida"** (we prepare everything, the user uploads in YouTube Studio, we link the video afterwards). API upload ("Publicación automática") is enabled when the user ticks *"Mi proyecto de Google ha superado la auditoría de YouTube"* **or** passes the in-app *prueba de bloqueo* (§3.7: one throw-away **unlisted** upload whose privacy reads back `unlisted` after processing); we always read back `status.privacyStatus` and flag a lock. |
| OAuth client | Google supports **Web application** clients with `http://localhost:<port>/<path>` redirects (localhost is exempt from the HTTPS rule; exact match) and **Desktop app** clients (loopback, any port, no registration). PKCE S256 base64url accepted by both. `client_secret` is required at the token endpoint for every client type. | Instruct the user to create a **Web application** client with redirect `http://localhost:8765/conexiones/callback/google` (deterministic exact match). Desktop clients also work with the same code (path behaviour UNVERIFIED). Always send PKCE S256 (base64url, **not** hex like TikTok). |
| Publishing status | **Testing**: ≤100 listed test users, grant + refresh token **expire 7 days** after consent (measured `refresh_token_expires_in: 604799`). **In production, unverified**: "Google hasn't verified this app" warning, lifetime cap 100 users, long-lived refresh tokens. Personal-use apps (<100 users) are exempt from verification. | Recommend **In production + unverified** (click through the warning once per connection). Testing only works if the user accepts reconnecting every week. Never ask a personal user to do brand/sensitive-scope verification. |
| Scopes | All YouTube scopes are *sensitive* (not restricted). `commentThreads.*`/`comments.*` accept **only** `youtube.force-ssl`; `channels.list` does **not** accept `youtube.upload`; Analytics accepts `yt-analytics.readonly` or `youtube.readonly`. Granular consent lets the user untick scopes. | Request `youtube.upload`, `youtube.readonly`, `youtube.force-ssl`, `yt-analytics.readonly`; store the granted `scope` string and enable features per scope. |
| Upload | Resumable: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status[,paidProductPlacementDetails]&notifySubscribers=…` → `Location` session URI → `PUT` chunks (multiples of 256 KiB) with `Content-Range` → `308` + `Range` until the last byte → `200/201` + video resource. **The video only exists after the last byte.** Status query: `PUT` with `Content-Range: bytes */TOTAL`. | Persist session URI right after init; 8 MiB chunks; `before_commit` immediately before the **final** chunk; reconcile = status query on the session URI. Never create a second session once the commit was sent. |
| Shorts | No API flag. A video uploaded after 2024-10-15 that is **square or vertical and ≤ 3 min** is classified as a Short automatically; `#Shorts` not required. | Two formats `video` / `short`; `short` validates aspect (h ≥ w) and duration ≤ 180 s; `video` warns when the file would become a Short anyway. Do **not** append `#Shorts` (Brightbean still does — outdated). |
| Scheduling | `status.publishAt` (ISO 8601) works only with `privacyStatus=private` and a never-published video; YouTube flips it to public at that time even if our PC is off. Past time ⇒ publishes immediately. For an **unaudited** project the video stays locked private (expected; UNVERIFIED end-to-end). | Option `native_schedule` ("Programar en YouTube"), default **on** for audited projects when scheduled ≥ 15 min ahead: we upload early and return `published` with `state.native_scheduled=true` (same pattern as SPEC §10 Facebook). Otherwise local scheduler (PC must be on). |
| Quotas | Default per project: **10,000 units/day** for all methods except `videos.insert` and `search.list`, which since **2026-06-01** have their **own buckets of 100 calls/day (1 unit each)**. Earlier: `videos.insert` 1600 → ~100 units (2025-12-04). Reset midnight Pacific. Writes (thumbnails.set, playlistItems.insert, comments.insert, commentThreads.insert, videos.update) = 50 units; lists = 1. | One user ≈ < 1,000 units/day. Never call `search.list` in normal operation (use the uploads playlist). On `quotaExceeded` → `RATE_LIMITED` with `retry_after` = next Pacific midnight; keep a per-bucket breaker (upload vs data vs analytics). |
| Thumbnails | `thumbnails.set` (`POST https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=…`), JPEG/PNG, needs a **verified channel** (phone). Max size raised from **2 MB → 50 MB** (discovery maxSize 2097152 → 52428800 between revs 20250714 and 20260820). | Optional "Miniatura personalizada" (only for format `video`); we still re-encode to JPEG ≤ 2 MB, 1280×720 for safety; failure is a **warning** after the video is live, never a publish failure. |
| Processing | `videos.list?part=status,processingDetails&id=…` (owner-only parts): `uploadStatus` `uploaded`/`processed`/`failed`/`rejected`/`deleted`; `failureReason`, `rejectionReason`, `processingDetails.processingStatus`. | `wait` loop polling every 30–60 s (1 unit each) for up to 30 min, then `published` with "YouTube sigue procesando"; `failed`/`rejected` → error with Spanish reason. |
| Analytics | Data API: `channels.list part=statistics` (subscriberCount **rounded to 3 significant figures**, viewCount, videoCount, hiddenSubscriberCount), `videos.list part=statistics` (≤50 ids/1 unit). Analytics API: `GET https://youtubeanalytics.googleapis.com/v2/reports?ids=channel==MINE&startDate&endDate&metrics&dimensions=day|video` — data lags ~2–3 days, Pacific dates. Developer Policies forbid **derived metrics** (e.g. engagement rate) unless approved. | Daily job: lifetime counters from Data API + re-fetch the last 7 days of `dimensions=day` from Analytics (upsert). Per-video: Data API counters + Analytics `dimensions=video&filters=video==…` (≤500 ids). Show native metrics only for YouTube (no computed engagement). |
| Comments | `commentThreads.list?part=snippet,replies&allThreadsRelatedToChannelId=UC…&order=time&maxResults=100` (still in the Sept-2026 discovery doc; 1 unit/page); reply = `comments.insert` `{snippet:{parentId, textOriginal}}` (50 units). Scope `youtube.force-ssl` only. | Poll every 15 min with early stop (newest-first + 2 h lookback, page cap); reply always to the **top-level** comment id. |

---

## 1. Google Cloud project setup

### 1.1 APIs to enable (in the user's project)

| API | Service name | Needed for |
|---|---|---|
| YouTube Data API v3 | `youtube.googleapis.com` | upload, channel/video/playlist reads, thumbnails, comments |
| YouTube Analytics API | `youtubeanalytics.googleapis.com` | daily/per-video analytics (views, watch time, subs gained/lost…) |

If an API is not enabled, calls fail with **403** (`reason: accessNotConfigured`, status `PERMISSION_DENIED`,
message "…has not been used in project … or it is disabled") — UNVERIFIED exact reason string; map any 403 whose
message contains "has not been used in project" to a Spanish setup error ("Activa *YouTube Analytics API* en tu
proyecto de Google Cloud").

### 1.2 Scopes (from `youtube.v3.json` rev 20260924 and `youtubeAnalytics.v2.json`)

| Scope | Consent text (discovery) | Methods that accept it (relevant subset) |
|---|---|---|
| `https://www.googleapis.com/auth/youtube.upload` | "Manage your YouTube videos" | `videos.insert`, `thumbnails.set` — **not** `channels.list`, `videos.list`, `playlistItems.insert`, comments |
| `https://www.googleapis.com/auth/youtube.readonly` | "View your YouTube account" | `channels.list`, `videos.list`, `playlists.list`, `playlistItems.list`, `videoCategories.list`, `search.list`, `videos.batchGetStats`, **Analytics `reports.query`** |
| `https://www.googleapis.com/auth/youtube.force-ssl` | "See, edit, and permanently delete your YouTube videos, ratings, comments and captions" | everything above incl. `videos.insert`, `videos.update`, `thumbnails.set`, `playlistItems.insert`, and it is the **only** scope accepted by `commentThreads.list/insert`, `comments.list/insert` |
| `https://www.googleapis.com/auth/youtube` | "Manage your YouTube account" | broad superset (not needed) |
| `https://www.googleapis.com/auth/yt-analytics.readonly` | "View YouTube Analytics reports for your YouTube content" | Analytics `reports.query` |
| `https://www.googleapis.com/auth/yt-analytics-monetary.readonly` | monetary + non-monetary reports | revenue metrics (out of scope) |

Facts:
- `commentThreads.list` scopes = `["youtube.force-ssl"]` only; `comments.insert` = `["youtube.force-ssl"]` only
  (discovery). `channels.list` scopes = youtube, force-ssl, readonly, youtubepartner, youtubepartner-channel-audit
  (no `upload`). `reports.query` scopes = youtube, youtube.readonly, youtubepartner, yt-analytics-monetary.readonly,
  yt-analytics.readonly.
- All YouTube user-data scopes are classified **sensitive** (not restricted) — third-party consensus + a Google
  forum post listing `youtube`, `youtube.force-ssl` as "sensitive scopes explicitly approved" (§16). Sensitive ⇒
  no CASA security assessment, only brand/scope verification if you want to remove the warning.
- Google's **granular consent** screen appears when an app requests more than one non-sign-in scope: the user can
  untick individual scopes, and apps "must check what scopes are granted by the users and can't assume users grant
  all requested scopes" (Google "How to handle granular permissions", §16). The token response `scope` field lists
  the granted scopes (Postiz checks them with `getTokenInfo` + `checkScopes`, `youtube.provider.ts:321-324`).

What the references request:
- Brightbean: `youtube.upload`, `youtube.readonly`, `youtube.force-ssl` (+ `yt-analytics.readonly` when analytics is
  enabled) — `providers/youtube.py:152-168`.
- Postiz: `userinfo.profile`, `userinfo.email`, `youtube`, `youtube.force-ssl`, `youtube.readonly`,
  `youtube.upload`, `youtubepartner`, `yt-analytics.readonly` — `youtube.provider.ts:64-73` (over-broad).
- OpenPost: `youtube.readonly`, `yt-analytics.readonly`, `youtube.upload`, `youtube`, `youtube.force-ssl` —
  `youtube.go:1353-1361`; its own review notes warn that requesting both narrow scopes and broad `youtube` is a
  verification risk (`provider-production-review-requirements-2026-09-19.md:117-123`).

**Decision:** request exactly
`youtube.upload youtube.readonly youtube.force-ssl yt-analytics.readonly` (no `openid/email/profile` — the channel
comes from `channels.list`). Feature gating from the granted set:

| Feature | Needs |
|---|---|
| Connect / channel info / video stats / recent posts | `youtube.readonly` **or** `youtube.force-ssl` |
| Publish (API mode) + thumbnail | `youtube.upload` **or** `youtube.force-ssl` |
| Playlist insert | `youtube.force-ssl` (or `youtube`) |
| Comments read/reply, first comment | `youtube.force-ssl` |
| Analytics API | `yt-analytics.readonly` **or** `youtube.readonly` |

### 1.3 OAuth client type and redirect URI

Google's redirect rules (official "Manage OAuth Clients", support.google.com/cloud/answer/15549257, via search):
"Redirect URIs must use the HTTPS scheme, not plain HTTP. **Localhost URIs (including localhost IP address URIs)
are exempt from this rule.**" Hosts cannot be raw IPs (localhost IPs exempt); no wildcards, fragments, userinfo,
path traversal; exact-match comparison (trailing slash matters — WorkOS 2026 blog, secondary).

| | **Web application** client | **Desktop app** client |
|---|---|---|
| Redirect registration | Must register the exact URI, e.g. `http://localhost:8765/conexiones/callback/google` | No redirect URIs in the console; Google accepts loopback redirects `http://127.0.0.1:<port>`, `http://[::1]:<port>`, `http://localhost:<port>` (Google loopback migration guide lists these three forms) on any port (RFC 8252 §7.3) |
| Path | Any path, exact match (proven in practice: `porjo/youtubeuploader` README:25-26 uses a *Web application* client with `http://localhost:8080/oauth2callback`) | Google's own helper uses the root path `http://localhost:<port>/` (`google-auth-oauthlib` 1.5.0 `flow.py:377-453`). A non-root path is **UNVERIFIED** |
| `client_secret` | Confidential in Google's model; required at token endpoint | "Not treated as a secret" but **still required** at the token endpoint ("Google requires client_secret at the token endpoint for every client type, including Web clients using PKCE" — npm runbook `auth.md:198-215`) |
| PKCE | Accepted (Google's `Flow` adds `code_challenge` S256 by default for any client type, `flow.py:236-257`) | Recommended |
| Loopback status | n/a | Loopback flow deprecated for iOS/Android/Chrome clients but "will continue to be supported on desktop apps" (Google loopback migration guide) |

Notes:
- The npm runbook claims "Never `localhost`… the hostname is matched literally" for Desktop clients
  (`auth.md:241-243`), but Google's own `InstalledAppFlow.run_local_server()` defaults to `host="localhost"`
  (`flow.py:377`) and is the standard way to use Desktop clients → we consider `localhost` accepted; path remains the
  open point.
- Our server binds only `127.0.0.1` while the browser resolves `localhost` (Windows may try `::1` first and fall
  back). `google-auth-oauthlib` reserves `::1:<port>` so another local process cannot intercept the callback
  (`flow.py:490-545`). PKCE makes an intercepted code useless without the verifier, so we accept the residual risk.
- SPEC §2 requires the hostname `localhost` in redirect URIs (cookies). With a Web client this is natural.

**Decision:** redirect URI = `f"{settings.base_url}/conexiones/callback/google"` =
`http://localhost:8765/conexiones/callback/google`. Setup guide asks for a **Web application** client and shows this
URI with a copy button. If the user created a Desktop client instead, the same code path is tried (Google ignores
registration for Desktop clients); on `redirect_uri_mismatch` show "Crea un cliente de tipo *Aplicación web* y
añade esta URI". (No Google rule forbids "google" in the path; if the console ever rejects it, fall back to
`/conexiones/callback/social2`.)

### 1.4 Publishing status: Testing vs In production (unverified) vs verified

Official (support.google.com/cloud/answer/15549945 "Manage App Audience", answer/13464321, production-readiness
overview — via search):
- **Testing**: "limited to up to 100 test users listed in the OAuth consent screen. Authorizations by a test user
  will expire seven days from the time of consent. If your OAuth client requests an offline access type and receives
  a refresh token, that token will also expire." Exception only for `openid/email/profile`-only apps.
  Empirical: token response in Testing contained `"refresh_token_expires_in": 604799` (6.99 days) and
  `"expires_in": 3599` (npm runbook `auth.md:302-313`, recorded 2026-07-18).
- **In production, unverified** (sensitive scopes not approved): users see the "unverified app" warning ("Danger
  UI"); "a hard cap of 100 total users applies"; the cap "applies over the entire lifetime of the project, and it
  cannot be reset". Refresh tokens are long-lived (no 7-day expiry).
- **Exemptions**: Google exempts "Personal Use apps … fewer than 100 users" from mandatory verification — users
  "click through the 'unverified app' warning screen during sign-in" (rclone `docs/content/drive.md:2058-2073`,
  citing support.google.com/cloud/answer/13464323).
- **Publish button**: "If the 'PUBLISH APP' button is greyed out, Google now requires a homepage URL and a privacy
  policy URL before publishing, even for a personal single-user app" — a free GitHub Pages site plus "Authorized
  domains" works (rclone `drive.md:2029-2039`). UNVERIFIED for every account, but plan the guide for it.
- **Internal** user type only exists for Google Workspace organisations (accounts of that org only; no 7-day expiry);
  rclone `drive.md:2022-2027`. Not applicable to @gmail.com users.
- **Verification** (brand + sensitive scopes) needs homepage + privacy policy on a verified domain, scope
  justifications and an English demo video showing every scope (OpenPost review notes `:115-125`). Not worth it
  for a personal tool, and it does **not** remove the YouTube private-upload lock (that is the separate YouTube API
  audit, §3).

| Choice | Pros | Cons |
|---|---|---|
| Testing (+ yourself as test user) | No homepage/privacy URL needed | **Reconnect every 7 days**; a post scheduled 8+ days ahead fails with `invalid_grant` |
| **In production, unverified** (recommended) | Refresh token lasts (until revoked / 6 months unused / 100-tokens-per-client limit) | Scary warning on every connect ("Configuración avanzada → Ir a … (no seguro)"); may need a homepage + privacy-policy URL to press *Publish*; 100-user lifetime cap (irrelevant) |
| Verified | No warning | Weeks of review, demo video, domain — overkill |

**Decision:** guide = Production unverified; detect Testing by `refresh_token_expires_in` in the token response (if
present → store `refresh_expires_at` and show "Tu app de Google está en modo *Prueba*: tendrás que reconectar
cada 7 días. Publícala (En producción) para evitarlo."). Refresh 24 h before expiry is impossible (consent needed), so
notify at T-48 h.

### 1.5 Setup guide text for *Ajustes → YouTube* (Spanish, proposed)

1. Entra en <https://console.cloud.google.com/>, crea un proyecto (p. ej. "Planificador").
2. *APIs y servicios → Biblioteca*: habilita **YouTube Data API v3** y **YouTube Analytics API**.
3. *Google Auth Platform* (pantalla de consentimiento): tipo **Externo**; nombre de la app, correo de asistencia.
   En *Acceso a datos* añade los permisos `…/auth/youtube.upload`, `…/auth/youtube.readonly`,
   `…/auth/youtube.force-ssl`, `…/auth/yt-analytics.readonly`. En *Público* añádete como usuario de prueba y pulsa
   **Publicar aplicación** (si el botón está gris, rellena en *Branding* una página de inicio y una política de
   privacidad — sirve una página gratuita de GitHub Pages — y su dominio autorizado).
4. *Clientes → Crear cliente* → tipo **Aplicación web** → *URI de redireccionamiento autorizados*:
   `http://localhost:8765/conexiones/callback/google` (botón Copiar). Copia el **ID de cliente** y el **Secreto**.
5. Pega ambos aquí y pulsa **Conectar YouTube**. Google mostrará "Google no ha verificado esta aplicación": pulsa
   *Configuración avanzada → Ir a Planificador (no seguro)*, elige el canal y **marca todas las casillas**.
6. (Opcional, para publicar en público desde la app) Solicita la auditoría de YouTube API Services (§3.5).

---

## 2. OAuth 2.0 flow (authorization code + PKCE)

### 2.1 Endpoints

| Purpose | Method + URL | Source |
|---|---|---|
| Authorize | `GET https://accounts.google.com/o/oauth2/v2/auth` | Brightbean `youtube.py:39`, OpenPost `youtube.go:22` |
| Token / refresh | `POST https://oauth2.googleapis.com/token` (form-encoded) | Brightbean `:40`, OpenPost `:23` |
| Revoke | `POST https://oauth2.googleapis.com/revoke` with form `token=<access or refresh token>` | Brightbean `:41`, `:1065-1076` |
| Data API base | `https://www.googleapis.com/youtube/v3` (discovery rootUrl is `https://youtube.googleapis.com/`; both serve) | all refs; discovery |
| Upload base | `https://www.googleapis.com/upload/youtube/v3` (discovery: simple path `/upload/youtube/v3/videos`, resumable `/resumable/upload/youtube/v3/videos`) | Brightbean `:43`, OpenPost `:26`, Postiz `:590` |
| Analytics base | `https://youtubeanalytics.googleapis.com/v2` | discovery, Brightbean `:44` |

### 2.2 Authorize parameters

| Param | Value |
|---|---|
| `client_id` | user's client id |
| `redirect_uri` | `http://localhost:8765/conexiones/callback/google` |
| `response_type` | `code` |
| `scope` | space-separated list from §1.2 |
| `state` | random, stored in `oauth_states` (SPEC §6) |
| `access_type` | `offline` (refresh token) |
| `prompt` | `consent select_account` — `consent` forces a new refresh token every time (Google only returns one on first consent otherwise; Brightbean `:198-199`, Postiz `:303-305`, OpenPost `:64-65`); `select_account` lets the user pick another Google account/channel when adding a second channel |
| `include_granted_scopes` | `true` (optional; OpenPost `youtube.go:66`) |
| `code_challenge`, `code_challenge_method` | PKCE: `S256`, `code_challenge = base64url(sha256(verifier))` **without `=` padding**; verifier 43–128 chars from `[A-Za-z0-9-._~]` (Google's helper uses 128 chars, `flow.py:244-257`). **Not hex** (TikTok-specific). |

None of the three references sends PKCE to Google (Postiz generates a `codeVerifier` but `generateAuthUrl` does not
pass a challenge, `:299-313`); PKCE is optional for confidential clients but costs nothing.

### 2.3 Token exchange

`POST https://oauth2.googleapis.com/token`, `Content-Type: application/x-www-form-urlencoded`:
`code`, `client_id`, `client_secret`, `redirect_uri` (identical to the authorize one), `grant_type=authorization_code`,
`code_verifier`.

Response (JSON): `access_token`, `expires_in` (3599 s), `token_type` (`Bearer`), `scope` (granted, space-separated),
`refresh_token` (only with `access_type=offline`; always present with `prompt=consent`), and
`refresh_token_expires_in` **when the grant is time-limited** (Testing status: 604799; Google's newer "time-based
access" option — field documented by Google only for that case; UNVERIFIED wording). Errors: `{"error": "...",
"error_description": "...", "error_subtype": "..."}` (OpenPost `google_oauth.go:22-37`).

### 2.4 Refresh

Same endpoint, form: `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`. Returns a new
`access_token` + `expires_in` (+ `scope`); Google normally does **not** rotate the refresh token — keep the old one
unless a new one is returned (Brightbean `youtube.py:248-251`, Postiz `:291`).

Refresh errors:
- `invalid_grant` ("Token has been expired or revoked.") → `AUTH` → account `needs_reconnect`. Causes: Testing 7-day
  expiry, user revoked access, refresh token unused for **6 months**, more than **100 live refresh tokens per Google
  account per OAuth client** (oldest silently invalidated), password change (only for Gmail scopes) — Google docs as
  quoted by UiPath/Nango/Unified (§16; secondary).
- `invalid_grant` with `error_subtype: invalid_rapt` → Google Workspace session-control policy forces re-auth
  (Postiz `:263-274`, OpenPost `google_oauth.go:84-93`). Spanish: "Tu organización de Google Workspace exige volver a
  iniciar sesión; reconecta YouTube."
- `invalid_client` / `unauthorized_client` → wrong client id/secret in Ajustes → `AUTH` with a settings message.

### 2.5 Revoke

`POST https://oauth2.googleapis.com/revoke` (form `token=`); revoking the refresh token kills the whole grant.
Treat 200 as success and 400 `invalid_token` as "already revoked". Brightbean revokes after local cleanup and retries
transient failures from an outbox for 7 days (`youtube-audit-remediation.md:101-103`) — for us: best effort on
"Desconectar", ignore failures.

### 2.6 Token lifetimes summary

| Token | Lifetime |
|---|---|
| Access token | 3600 s (`expires_in: 3599`) |
| Refresh token, Testing status | 7 days from consent |
| Refresh token, In production | until revoked / 6 months unused / per-client token limit / Workspace policy |

`refresh_credentials`: refresh when `token_expires_at - now < 5 min`, before every publish step that calls the API,
and on any 401 (once). Uploads longer than 1 h must refresh mid-session — the session URI stays valid; send the new
Bearer token on the next `PUT` (Postiz and OpenPost send `Authorization` on every chunk, `youtube.provider.ts:745-756`,
`youtube.go:998-1003`).

### 2.7 Channel discovery after OAuth

`GET /youtube/v3/channels?part=snippet,contentDetails,statistics,status&mine=true&maxResults=50` (1 unit).

| Field | Use |
|---|---|
| `id` (`UC…`) | `accounts.external_id` |
| `snippet.title`, `snippet.customUrl` (`@handle`), `snippet.thumbnails.default/medium.url` | name, handle, avatar |
| `snippet.country`, `snippet.defaultLanguage` | default region for `videoCategories.list` |
| `contentDetails.relatedPlaylists.uploads` (`UU…`) | uploads playlist for recent posts / reconcile |
| `statistics.subscriberCount`, `viewCount`, `videoCount`, `hiddenSubscriberCount` | analytics snapshot |
| `status.longUploadsStatus` (`allowed`/`eligible`/`disallowed`/`longUploadsUnspecified`), `status.privacyStatus`, `status.isLinked`, `status.madeForKids` | validation: videos > 15 min need `allowed` |

- `items` empty ⇒ the Google account has no YouTube channel (OpenPost error "google account has no YouTube channels",
  `youtube.go:196`); uploading would fail with `youtubeSignupRequired`. Spanish: "Esta cuenta de Google no tiene
  canal de YouTube. Créalo en youtube.com y vuelve a conectar."
- **Brand accounts / multiple channels:** Google's consent shows an account/channel picker; in practice the token is
  bound to the channel chosen there and `mine=true` returns that single channel (UNVERIFIED; references still handle a
  list: OpenPost lets the user pick, `youtube.go:127-170`; Brightbean pages through `mine=true` results,
  `youtube.py:260-301`). `managedByMe` is for content owners only. To add a second channel, run the flow again
  (`prompt=select_account`) and choose the other channel; each channel row stores its own refresh token.
- Store in `accounts.meta`: `channel_id`, `handle`, `uploads_playlist_id`, `long_uploads_status`, `granted_scopes`,
  `client_status_hint` (`testing` if `refresh_token_expires_in` was present), `upload_lock_suspected` (§3.4).

---

## 3. Unverified (unaudited) API projects: uploads locked to private — CRITICAL

### 3.1 The rule (official 2020; contradicted by the reference text since 2026-10-08)

YouTube Data API revision history, **2020-07-28** (quoted verbatim in `porjo/youtubeuploader` README:45-47, linking
`developers.google.com/youtube/v3/revision_history#july-28,-2020`; also on the `videos.insert` and `videos` reference
pages per search snippets, Oct 2026):

> "All videos uploaded via the videos.insert endpoint from unverified API projects created after 28 July 2020 will be
> restricted to private viewing mode. To lift this restriction, each project must undergo an audit to verify
> compliance with the Terms of Service."

**Contradiction found in the fact-check (2026-10-15).** Google's machine-readable docs changed between discovery
rev 20260924 and rev **20261012**. `VideoStatus.privacyStatus` used to be described only as "The video's privacy
status." It now reads:

> "The video's privacy status. In a `videos.insert` request, if you don't specify a value for this property, the
> uploaded video's privacy status defaults to `public`. Certain uploads might default to `private` based on creator
> account settings, as explained in the YouTube Help Center (answer/157177). **Videos uploaded from unverified API
> projects are not restricted to private viewing mode.**"

Sources: `google-api-go-client` main `youtube/v3/youtube-api.json` (rev 20261012); npm `@googleapis/youtube` 40.0.1
(published 2026-10-14) `build/v3.d.ts:4746`, absent in 40.0.0 (2026-10-03) and 39.0.1;
`googleapis/google-api-java-client-services` and `google-api-php-client-services` `VideoStatus` (GitHub code search).
Several independent projects quote the same sentence from the live `videos.insert` and `videos` reference pages, which
say "last updated 2026-10-08" (`antonuemad/repeter` `docs/research/youtube.md`, `skalenetwork/agentpit-bench`
`docs/youtube-shorts-spec.md`, `23abdul23/YT` `docs/research/debate/D7-A.md`/`D7-B.md`). On the other side: the
revision history (scraped copy fetched 2026-09-08, `mattmezstitchlab/byaime-one-page`
`research/sources/freshness-03-youtube-revision-2026.md:849-866`) still carries the 2020 entry and no reversal entry
up to 2026-09-01; YouTube Help answer/7300965 still lists "uploaded by an unverified third party API service";
third-party guides from Aug–Oct 2026 still report locks. No first-hand 2026 report either way was found.
⇒ **Treat the lock as possible but no longer certain.** It may have been lifted (quietly, docs-first), or the new
sentence may be a documentation error. Only a real upload settles it (§3.7 *prueba de bloqueo*).

- Existing (pre-2020-07-28) projects are not affected. Any project the user creates now **was** affected under the
  2020 rule (see the contradiction above).
- Setting `privacyStatus: "public"` does not override it and the call does not fail (third-party guides; consistent
  with the reference implementations that only document the caveat: OpenPost "Unaudited Google projects can force
  uploads private", `capabilities.go:462-463`; OpenPost docs "Uploads videos as private by default",
  `docs/reference/providers/youtube.md:44`).
- Quota is unrelated: staying under quota does not lift the lock.
- OAuth app verification (§1.4) is a **different** process and does not lift it (OpenPost review notes `:127-131`:
  "OAuth verification is not the whole YouTube launch… forced to private until the project passes a YouTube API
  Services compliance audit").

### 3.2 What the creator sees

YouTube Help "Videos locked as private" (support.google.com/youtube/answer/7300965, via search): content "uploaded by
an unverified third party API service" is **locked as private**; "you will not be able to appeal, and you'll need to
re-upload the video via a verified API service or via the YouTube app/site"; no strike is applied. The creator gets an
e-mail saying the video is locked "because it was uploaded by a third-party service that hasn't yet been through our
verification process" (wording seen in `porjo/youtubeuploader` issue #86, via search).

⇒ Unlike TikTok `SELF_ONLY` (the creator can change visibility in the TikTok app), a locked YouTube video **cannot be
made public by the creator**. An unaudited API upload is only useful as a private file.

### 3.3 Interaction with `publishAt`

Expected: the video stays private at `publishAt` (lock wins). One developer test reported a private+`publishAt` video
still private with `publishAt` retained; another project treats "still private 15 min after publish time" as the
unaudited signal; a third project reports scheduled videos going public on time on an (apparently) audited project
(GitHub search snippets, §16). **UNVERIFIED** end-to-end.

### 3.4 Detecting the lock

There is no documented API field. Practical signals:
1. The `videos.insert` response `status.privacyStatus` differs from what we sent (`public`/`unlisted` requested,
   `private` returned) — UNVERIFIED that the lock is visible already in the insert response.
2. After processing, `videos.list part=status` still shows `private` and (for `publishAt`) the time has passed.
3. A `videos.update` to public is ignored or rejected (UNVERIFIED behaviour; we will not attempt it automatically —
   it costs 50 units and could anger the reviewer later).

Decision: on signal 1 or 2, mark the target `published` with a **warning event** ("YouTube ha bloqueado el vídeo como
privado porque tu proyecto de Google no está auditado"), set `accounts.meta.upload_lock_suspected=true`, and switch
the account's default mode back to *Publicación asistida*.

### 3.5 The audit (YouTube API Services Audit and Quota Extension Form)

- Form: `https://support.google.com/youtube/contact/yt_api_form` ("YouTube API Services - Audit and Quota Extension
  Form"); a shorter form exists for projects audited compliant in the last 12 months; appeals and change-of-control have
  separate forms; the guide is developers.google.com/youtube/v3/guides/quota_and_compliance_audits (via search).
- No published timeline ("weeks to months", third-party). Individuals can apply (an integration wiki tells customers to
  enter their own legal name; anecdotal). OpenPost suggests describing the product as a "user-directed publishing
  client", listing endpoints, storage, consent, revocation and deletion, and **not** as an internal tool
  (`provider-production-review-requirements-2026-09-19.md:131`). Approval of a single-user personal tool is
  **UNVERIFIED** — no first-hand report found.
- Passing the audit later does not un-lock videos already uploaded (third-party reading; YouTube says re-upload).
- Brightbean's Oct-2026 reviewer report shows what auditors check: no derived metrics such as engagement rate
  (policy III.E.4.h), YouTube branding/attribution next to YouTube data (III.F.2.a), refresh/delete stored API data
  (copies refreshed by day 25, deleted by day 29; statistics history kept 180 days), immediate purge + revocation on
  disconnect (III.E.4.a–g), project inventory (`youtube-audit-remediation.md:16-123`).

### 3.6 How other tools cope

| Tool | Behaviour |
|---|---|
| Postiz | Hosted SaaS with its own (presumably audited) project; self-hosters get private uploads; no special UX. |
| Brightbean | Hosted; was going through the audit in Oct 2026 (`youtube-audit-remediation.md`); no unaudited UX. |
| OpenPost | Caveat text on both YouTube formats ("Unaudited Google projects can force uploads private"), default privacy private, review notes. |
| youtubeuploader (CLI) | README NOTE 1 + default `-privacy private`. |
| Metricool & co. | Use their own audited projects (the user does not bring credentials) — not an option for us. |

### 3.7 Decision for our UX

- New setting per Google client: **"Mi proyecto de Google ha superado la auditoría de YouTube API Services"**
  (`youtube_project_audited`, default **false**), with a link "¿Qué es esto?" explaining §3.1–3.2 in Spanish.
- **Prueba de bloqueo** (added after the fact-check, because Google's docs now say the lock does not apply): a button
  "Probar si YouTube bloquea mis subidas" uploads a 5-second generated clip as **unlisted** (`privacyStatus=unlisted`,
  title "Prueba Planificador — puedes borrarlo", `notifySubscribers=false`, `selfDeclaredMadeForKids=false`), waits
  for processing, reads `status.privacyStatus` back, then offers `videos.delete` (≈ 1 upload-bucket call + ~60 units).
  Result `unlisted` after processing → store `youtube_upload_lock=false` and allow *Publicación automática* (same
  effect as ticking "auditado"); result `private` (or the "locked as private" e-mail) → `youtube_upload_lock=true`, keep
  assisted mode. Re-check automatically on every real upload (§3.4). Whether a lock shows up within minutes (vs. later)
  is UNVERIFIED, so re-read the test video's status after 24 h before deleting it.
- When **false** (default):
  - YouTube targets default to `auto_publish=False` → SPEC §10 *Publicación asistida* at the scheduled time: modal
    with "Abrir YouTube Studio" (`https://studio.youtube.com/channel/<UC…>/videos/upload?d=ud`; UNVERIFIED deep-link,
    fallback `https://studio.youtube.com/`), "Descargar vídeo", "Copiar título", "Copiar descripción", "Copiar
    etiquetas", the chosen options as a checklist (visibilidad, niños, contenido alterado, miniatura) and "Marcar como
    publicado". On the next `fetch_recent_posts`, auto-link the new upload to the target by exact title within ±48 h
    (sets `external_id`, so analytics and comments attach).
  - Automatic API upload remains available as an explicit opt-in per post: "Subir con la API (se quedará **privado y
    bloqueado** por YouTube)". Useful only for testing.
  - Analytics and comments work normally (reads are not affected by the audit).
- When **true**: normal automatic publishing, native scheduling, warnings from §3.4 still active.

---

## 4. Upload: `videos.insert` with the resumable protocol

### 4.1 Discovery facts (`youtube.v3.json` rev 20260924)

- `youtube.videos.insert`: `POST youtube/v3/videos`, `supportsMediaUpload`, `mediaUpload.accept = ["video/*",
  "application/octet-stream"]`, **`maxSize = 274877906944` (256 GiB)**, protocols `simple` (`/upload/youtube/v3/videos`)
  and `resumable` (`/resumable/upload/youtube/v3/videos`), both `multipart: true`.
- Query parameters: `part` (required, repeated), `notifySubscribers` (boolean, **default `true`**, "Notify the channel
  subscribers about the new video"), `autoLevels`, `stabilize` (booleans, legacy), `onBehalfOfContentOwner[Channel]`
  (partners only).
- Scopes: `youtube`, `youtube.force-ssl`, `youtube.upload`, `youtubepartner`.

### 4.2 Step 1 — start the session

```
POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status,paidProductPlacementDetails&notifySubscribers=true
Authorization: Bearer <access_token>
Content-Type: application/json; charset=UTF-8
X-Upload-Content-Length: <file size in bytes>
X-Upload-Content-Type: video/mp4            (actual mime; video/* accepted)

{ "snippet": {...}, "status": {...}, "paidProductPlacementDetails": {"hasPaidProductPlacement": false} }
```

- Response `200 OK`, empty body, header **`Location: <session URI>`** (contains `upload_id=`). No video exists yet.
  Postiz `:589-623` (`part=id,snippet,status&notifySubscribers=true`), OpenPost `:824-855` (`part=snippet,status,
  paidProductPlacementDetails`), googleapiclient `http.py:1082-1084` (X-Upload headers).
- Errors at this step are metadata/quota/auth errors (400 `invalidTitle` etc., 403 `quotaExceeded`, 401) and are
  **safe to retry** (no video created).
- Quota: 1 call of the `videos.insert` bucket (§9). UNVERIFIED whether the init or the final PUT is what is counted —
  irrelevant for us.

### 4.3 Video resource fields we send

From discovery schemas `VideoSnippet`, `VideoStatus`, `VideoPaidProductPlacementDetails` + limits from the official
`videos` resource page (via search and reference validators):

| Field | Type / values | Limit / rule | Our source |
|---|---|---|---|
| `snippet.title` | string, **required** (`invalidTitle` if empty) | ≤ **100 characters**, any valid UTF-8 **except `<` and `>`** | option `title` (fallback: first line of the text) |
| `snippet.description` | string | ≤ **5000 bytes** (UTF-8), no `<` `>` | `target.text` |
| `snippet.tags[]` | strings | total ≤ **500 characters**: commas between items count, and a tag containing a space is counted as if wrapped in quotes (+2) | option `tags` |
| `snippet.categoryId` | string id | must be assignable in the region (`videoCategories.list`) else `invalidCategoryId` | option `category_id` (default `"22"` People & Blogs) |
| `snippet.defaultLanguage` | BCP-47 | needed before `localizations` (`defaultLanguageNotSet`) | option `language` (optional) |
| `snippet.defaultAudioLanguage` | BCP-47 | — | same option |
| `status.privacyStatus` | `public` \| `unlisted` \| `private` | `publishAt` requires `private`; **omitted ⇒ `public`** on insert, "certain uploads might default to `private` based on creator account settings" (discovery rev 20261012) — always send it explicitly | option `privacy_status` |
| `status.publishAt` | RFC 3339 date-time | "can be set only if the privacy status of the video is private" (discovery) and the video has never been published; past ⇒ publishes now | `scheduled_at` when `native_schedule` |
| `status.selfDeclaredMadeForKids` | boolean | COPPA declaration (`status.madeForKids` is the read-only effective value) | option `made_for_kids` (**required choice**) |
| `status.containsSyntheticMedia` | boolean | "Indicates if the video contains altered or synthetic media" (added 2024-10-30) | option `synthetic_media` |
| `status.embeddable` | boolean | default true | option `embeddable` |
| `status.license` | `youtube` \| `creativeCommon` | — | option `license` (default `youtube`) |
| `status.publicStatsViewable` | boolean | — | not exposed (default) |
| `paidProductPlacementDetails.hasPaidProductPlacement` | boolean | must add `paidProductPlacementDetails` to `part` | option `paid_promotion` |
| `recordingDetails.recordingDate`, `.location` | — | `invalidRecordingDetails` | not exposed |

- When `selfDeclaredMadeForKids` is omitted the effective value comes from the channel default or stays "not set"
  for the creator to fix in Studio (UNVERIFIED). OpenPost sends it only when set (`youtube.go:472`, pointer); Postiz
  sends `yes/no` (`:609`). We make the UI choice mandatory and always send it.
- Validators in the refs: OpenPost description ≤ 5000 **bytes** and `<>` forbidden (`providerlimits/youtube.go:8-30`),
  title ≤ 100 runes (`youtube.go:27`); Postiz tags 500 incl. +2 for whitespace tags
  (`youtube.settings.dto.ts:19-29`); Brightbean caps total tag chars at 500 with commas (`validators.py:174-197`).
- YouTube Developer Policies: an API client "must not set a shorter maximum length" than YouTube's 100-char title, and
  the RMF requires letting users set **title, description and privacy** when uploading (official RMF/Developer
  Policies, via search). So: do not truncate silently; validate and show the error.

### 4.4 Step 2 — upload the bytes

```
PUT <session URI>
Authorization: Bearer <access_token>
Content-Type: video/mp4
Content-Length: <chunk length>
Content-Range: bytes <first>-<last>/<total>
<chunk bytes>
```

- Chunk size: a multiple of **256 KiB (262144 bytes)** for every chunk except the last (Google resumable-upload rule,
  stated by Postiz `:444`; the YouTube guide page itself did not surface in search — UNVERIFIED there, follow it
  anyway). Refs use **8 MiB** (Postiz `:445`, OpenPost `:924`). Google's own client default is 100 MiB
  (`http.py:67`); the official sample uploads in one request (`chunksize=-1`, `upload_video.py:83-94`).
- Responses:
  - **`308 Resume Incomplete`** + header `Range: bytes=0-<last received>` → next offset = last+1. No `Range` header ⇒
    nothing received yet (offset 0). Postiz `:758-771`, OpenPost `:1010-1029`, googleapiclient `http.py:1192-1199`.
    A `Retry-After` header on 308 means wait before resuming (YouTube resumable guide, translated versions, via search).
  - **`200 OK` / `201 Created`** + JSON **video resource** (`id`, `snippet`, `status` incl. `uploadStatus: "uploaded"`
    and the effective `privacyStatus`) → the video now exists.
  - `5xx` / `429` → transient: query status (§4.5) and resume (OpenPost gives up after 3 transient failures that make
    no progress, `:1030-1043`; official sample retries 500/502/503/504 up to 10 times with sleep
    `random()*2**retry`, `upload_video.py:23-33,101-132`).
  - `401` → refresh token, then status query + resume.
  - `404`/`410` on the session URI → session expired/unknown → must start over (OpenPost marks terminal,
    `:1108-1111`; Postiz message "The upload session expired before the video was uploaded", `:568-570`).
- Variant seen in Google's Go client (`google-api-go-client` `internal/gensupport/resumable.go:74-125`): chunks are sent
  with `POST` (not `PUT`) to the session URI, non-final chunks use `Content-Range: bytes <first>-<last>/*`, and the
  request header **`X-GUploader-No-308: yes`** makes Google answer `200 OK` + `X-HTTP-Status-Code-Override: 308`
  instead of a raw 308. Both forms work against Google's upload endpoint; we keep the documented `PUT` + known total
  and simply never let httpx follow redirects (§4.6). The Go client's minimum/default chunk are 256 KiB / 16 MiB
  (`googleapi/googleapi.go:55-62`), confirming the 256 KiB-multiple rule from an independent Google source.
- Session lifetime: "limited lifetime… will eventually expire" (YouTube guide, via search); Google's general resumable
  sessions last about a week; OpenPost stops automatic writes after **6 days** (`youtube.go:30-32`). We do the same.

### 4.5 Status query (resume / reconcile)

```
PUT <session URI>
Authorization: Bearer <access_token>
Content-Length: 0
Content-Range: bytes */<total>
```
→ `308` + `Range` (incomplete; resume from offset) | `200`/`201` + video resource (**already completed** — this is
the idempotency cure) | `404`/`410` (session gone). Postiz `probeUploadSession` `:515-572`, OpenPost
`queryYouTubeUploadState` `:1088-1112`, googleapiclient `http.py:1107`.

### 4.6 httpx notes (our stack)

- `httpx.Client` does not follow redirects by default; keep it so (a 308 must reach our code; Google's own client has
  to remove 308 from httplib2's redirect codes, `http.py:2029-2034`).
- Stream each chunk from disk with a generator and **set `Content-Length` explicitly** (otherwise httpx uses chunked
  transfer encoding). Never read the whole file into memory (Brightbean comment `:355-357`; OpenPost's
  `UploadMediaWithMetadata` reads all bytes into memory, `:343-365` — don't copy that).
- Timeouts: connect 15 s, write/read 120 s per 8 MiB chunk (SPEC §10 already sets 120 s read for uploads).

### 4.7 Idempotency and the commit point (maps to SPEC `before_commit`)

- YouTube creates the video **only when the final byte is received** (Postiz comment `:438-443`). Therefore:
  - Init + all non-final chunks are reversible: a crash there leaves at most an abandoned session.
  - The **final chunk** is the irreversible call → call `ctx.before_commit(state)` (with `session_uri`,
    `total_bytes`, `offset`) immediately before sending it.
  - Reconcile = status query on the saved session URI (§4.5): `200/201` ⇒ `published` (store id); `308` ⇒
    `not_published` (safe to resume the same session; never open a new one while the old session is alive);
    `404/410` ⇒ fall back to §4.8.
- Persist after each successful chunk: `uploaded_bytes` (from `Range`), but **always trust the server's `Range` over
  local state** after a crash.

### 4.8 Fallback when the session is gone

1. Read the uploads playlist: `playlistItems.list?part=contentDetails,snippet&playlistId=UU…&maxResults=10` (1 unit),
   then `videos.list?part=snippet,status&id=…` (1 unit).
2. Match `snippet.title == our title` and `snippet.publishedAt >= state.session_started_at - 5 min`.
3. Found ⇒ `published`; not found ⇒ `unknown` (the engine notifies; the user checks Studio). Whether the uploads
   playlist lists the owner's **private** videos is UNVERIFIED; alternative `search.list?forMine=true&type=video&
   order=date&maxResults=10` (search bucket, 100/day; discovery describes `forMine` as "Search for the private videos
   of the authenticated user") and `forDeveloper=true` ("only retrieve videos uploaded using the project id of the
   authenticated user") — use only in this rare path.

### 4.9 Post-upload steps (idempotent, flags in `provider_state`)

Order used by OpenPost (`youtube.go:544-573`): thumbnail → playlist → captions → processing check. Each step sets a
flag (`thumbnail_applied`, `playlist_applied`, `first_comment_id`) so a crash never repeats it; when resuming, check
remote existence first (playlist: `playlistItems.list?part=id&playlistId=…&videoId=…`, OpenPost `:1140-1159`).
Failures after the video exists must **not** fail the post (Postiz `PublishedWithError`, `:839-853`): record a warning
event and continue.

- Playlist: `POST /youtube/v3/playlistItems?part=snippet` body
  `{"snippet":{"playlistId":"PL…","resourceId":{"kind":"youtube#video","videoId":"…"}}}` (50 units; scopes youtube /
  force-ssl / youtubepartner — **not** upload). OpenPost `:1114-1138`.
- First comment (optional): `POST /youtube/v3/commentThreads?part=snippet` body
  `{"snippet":{"videoId":"…","topLevelComment":{"snippet":{"textOriginal":"…"}}}}` (50 units, force-ssl). OpenPost
  `first_comment_youtube.go:15-60`, Brightbean `:418-440`. Fails with `commentsDisabled` on made-for-kids videos and
  is pointless on private/locked videos — skip it unless privacy is public/unlisted or `publishAt` is set (then post
  it… only after the video is public? UNVERIFIED whether commenting on a scheduled private video works → skip for
  `native_schedule`, and offer it later via a follow-up job at `publishAt + 5 min`).

---

## 5. Shorts

- Classification (YouTube Help "Understand three-minute YouTube Shorts", answer/15424877, via search): "Videos
  uploaded after **October 15, 2024**: any videos uploaded on or after this date with a **square or vertical aspect
  ratio up to three minutes** in length will be categorized as Shorts." (Official Artist Channels: after 2025-12-08.)
  Older uploads keep the old 60 s rule.
- **No API flag**: `videos.insert` has no Shorts field (discovery has none); YouTube decides from the file. `#Shorts`
  in title/description is not required (still harmless). Brightbean appends ` #Shorts` to the title
  (`youtube.py:318-320`) — outdated, and it eats title characters.
- OpenPost's inference rule = height ≥ width **and** duration ≤ 180 000 ms, and only when both are known
  (`capabilities.go:1058-1071`); Short media constraints ≤ 180 s, aspect 9:16 or 1:1 (`:328-330`).
- Content ID: "Any Short that is over one minute in duration with an active Content ID claim of any type… will be
  blocked globally" (same Help page) → warning for Shorts > 60 s with music.
- Shorts views: since **2025-03-31** a Shorts "view" counts every start/replay; the old definition is the Analytics
  metric **`engagedViews`** (available in the Analytics API since **2025-04-30**; Analytics revision history, via
  search). Data API `viewCount` follows the new rule. **Update 2026-08-27** (Data API revision history): YouTube
  "will align public view counting for all video formats: long-form videos, Live, and Shorts" — a public view now
  counts "the moment a video begins to play" (first frame, incl. autoplay/hover); **Engaged View is unchanged**
  and drives monetization and most core analytics; `viewCount` descriptions in `channels`/`videos` were updated.
  ⇒ Show both "Visualizaciones" (`views`) and "Visualizaciones comprometidas" (`engagedViews`) for every YouTube
  format; whether the Analytics `views` metric itself switched on 2026-08-27 is UNVERIFIED.
- Thumbnails: a custom thumbnail may be set via `thumbnails.set` but is not shown in the vertical Shorts feed
  (shown in search, channel page, playlists); YouTube rolled out custom Shorts thumbnails to YPP creators in 2026
  (secondary). Hide the option for `short` or show "puede no verse en el feed de Shorts".
- URLs: `https://www.youtube.com/shorts/<id>` for Shorts, `https://www.youtube.com/watch?v=<id>` works for both — store
  the watch URL as `external_url`, display the shorts URL for format `short`.
- Detecting Shorts for analytics: Analytics dimension `creatorContentType` (values `LIVE_STREAM`, `SHORTS`, `STORY`,
  `VIDEO_ON_DEMAND`, `UNSPECIFIED` per CData's YouTube Analytics connector docs — third-party; Google's own enum
  page not read, so still UNVERIFIED) or simply our own format for posts we published.

---

## 6. Thumbnails: `thumbnails.set`

- `POST https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=<id>` with the image as body
  (`uploadType=media`, `Content-Type: image/jpeg|image/png`) — Brightbean `:385-396`, OpenPost `:797-822`. Discovery:
  `mediaUpload.accept = ["image/jpeg", "image/png", "application/octet-stream"]`, scopes youtube / force-ssl / upload /
  youtubepartner, response `ThumbnailSetResponse` (`items[]` = ThumbnailDetails).
- Max size: discovery `maxSize` **2097152 (2 MiB)** in rev 20250714 → **52428800 (50 MiB)** in revs 20260820 and
  20260924 (also `playlistImages.insert`), still 52428800 in rev 20261012 (Go client copy). The Data API revision
  history logs the 2 MB → 50 MB change on **2026-09-14** (search snippet; also cited in code by
  `MerlinStacks/socaliseit` `app/src/lib/platform-api/youtube-api.ts`); older mirrors still say 2 MB. → We keep **≤ 2 MB JPEG**, 1280×720 (16:9),
  min width 640 — valid under both limits.
- Requires a **verified channel** (phone verification at youtube.com/verify; "intermediate features"); otherwise
  `403 forbidden` ("The authenticated user doesn't have permissions to upload and set custom video thumbnails"). Postiz
  maps the `youtube.thumbnail` error domain to "custom thumbnails require a phone-verified YouTube channel"
  (`youtube.provider.ts:237-243`); Ayrshare: activation can take up to 24 h after verification (secondary).
- Other errors (Postiz `:181-219`, official list): 400 `imageFormatUnsupported`, `imageTooTall`, `imageTooWide`,
  `mediaBodyRequired`; 400 `failedPrecondition` (size); 404 `videoNotFound`; **429 `uploadRateLimitExceeded`** (too
  many thumbnails recently).
- Cost 50 units (Brightbean `:176-179`). Can be set right after upload (no need to wait for processing; Postiz does it
  immediately).

---

## 7. Native scheduling (`status.publishAt`)

- Discovery: "The date and time when the video is scheduled to publish. It can be set only if the privacy status of
  the video is private." Official resource page adds (via secondary quotes, consistent across sources): the video must
  never have been published; on `videos.update` you must resend `privacyStatus=private`; a past time publishes
  immediately. Format ISO 8601/RFC 3339 with offset or `Z` (OpenPost normalises to UTC, `youtube.go:445-455`).
- Behaviour: YouTube's servers flip the video to **public** at `publishAt` → works with the PC off. It always becomes
  **public** (no "scheduled unlisted"). Errors: 400 `invalidPublishAt`.
- Shorts can be scheduled the same way.
- Unaudited projects: video remains locked private (§3.3).
- Changing/cancelling later: `videos.update?part=status` with a new `publishAt` (+ `privacyStatus: private`) or
  `videos.delete` (both 50 units; scope force-ssl). v1: "Cancelar" on a natively scheduled YouTube target asks
  "¿Eliminar también el vídeo de YouTube?" and calls `videos.delete` only on confirmation.

**Decision** (mirrors SPEC §10 Facebook `native_schedule`):
- Option `native_schedule` (label "Programar en YouTube — se publica aunque tu PC esté apagado"), available when the
  project is audited, privacy = Pública and `scheduled_at - now ≥ 15 min`. Default **on** in that case.
- The engine must start such targets **early**: claim when `now >= scheduled_at - upload_lead` (lead = max(30 min,
  estimated upload time)) or simply immediately after saving. `publish_step` sends `privacyStatus=private`,
  `publishAt=scheduled_at` (UTC `Z`), returns `published` with `external_id`, `state.native_scheduled=true`; planner
  shows "Programado en YouTube". A periodic check after `publishAt` reads `status.privacyStatus` and records an event
  ("Publicado en YouTube" or "Sigue privado: posible bloqueo por proyecto sin auditar").
- If `scheduled_at` has already passed when the upload finishes, omit `publishAt` and send the requested privacy.

---

## 8. Processing status (`wait` polling) and reconcile reads

`GET /youtube/v3/videos?part=status,processingDetails&id=<id>` (1 unit; ≤ 50 ids). `processingDetails`, `fileDetails`,
`suggestions` are **owner-only** parts (discovery descriptions).

| Field (discovery) | Values |
|---|---|
| `status.uploadStatus` | `uploaded`, `processed`, `failed`, `rejected`, `deleted` |
| `status.failureReason` (only when failed) | `conversion`, `invalidFile`, `emptyFile`, `tooSmall`, `codec`, `uploadAborted` |
| `status.rejectionReason` (only when rejected) | `copyright`, `inappropriate`, `duplicate`, `termsOfUse`, `uploaderAccountSuspended`, `length`, `claim`, `uploaderAccountClosed`, `trademark`, `legal` |
| `processingDetails.processingStatus` | `processing`, `succeeded`, `failed`, `terminated` |
| `processingDetails.processingFailureReason` | `uploadFailed`, `transcodeFailed`, `streamingFailed`, `other` |
| `processingDetails.processingProgress` | `partsTotal`, `partsProcessed`, `timeLeftMs` (uint64 as strings) |
| `processingDetails.thumbnailsAvailability`, `fileDetailsAvailability`, … | availability flags |

- Right after upload `items` may be empty for a moment → treat as "not yet" (OpenPost `:1180-1185`).
- OpenPost readiness rule: ready when `processingStatus == succeeded` **or** `uploadStatus == processed`; terminal when
  `processingStatus == failed` or `uploadStatus in (rejected, failed, deleted)` (`youtube.go:1186-1199`).
- Our `wait` policy: 30 s, 30 s, 60 s, then every 120 s; stop after 30 min → `published` + message "YouTube sigue
  procesando el vídeo" (the video exists; failures will surface in the next analytics/reconcile pass). `failed`/
  `rejected` → `ProviderError(PERMANENT)` with Spanish reason (e.g. `duplicate` → "YouTube lo rechazó por ser un
  vídeo duplicado"; `length` → "supera la duración permitida para tu canal"); keep `external_id`.

---

## 9. Quotas

### 9.1 Model as of Oct 2026

| Date | Change (Data API revision history, via search; secondary confirmation from Brightbean) |
|---|---|
| ≤ 2025 | Single pool 10,000 units/day per project; `videos.insert` **1600** units, `search.list` 100 |
| **2025-12-04** | "documentation and quota calculator updated to reflect a cost change for video uploads from about 1600 units to about 100 units" |
| **2026-06-01** | `videos.insert` and `search.list` moved to **their own buckets**: default **100 calls/day each, 1 unit per call**; all other methods share **10,000 units/day**. Increases per bucket via the Quota Extension Form. Brightbean: "Since 2026-06-01 the Data API meters three daily budgets, each per Google Cloud project… `videos.insert` and `search.list` get 100 calls apiece at 1 unit a call" (`youtube.py:171-185`; `apps/common/quota.py:9-18,53-61`). The `videos.insert` reference now says "quota cost of 1 unit in the Video Uploads bucket" (search snippet). |
| 2026-06-03 | New `videos.batchGetStats` (`GET youtube/v3/videos:batchGetStats?part=…&id=…`, ≤ 50 ids, returns `statistics.viewCount/likeCount/commentCount`, `snippet.publishTime`, `contentDetails.duration`) with its own bucket: "quota cost of 1 unit in its own granular quota bucket. The method's default quota is **10,000 units per day**" (revision history, scraped copy 2026-09-08; method present in discovery rev 20260924, absent in 20250714). |

Caveats: some third-party pages and the calculator's summary text still show 1600; "not every existing Google Cloud
project was migrated identically" (bundle.social, Aug 2026) → show the user where to check (*APIs y servicios →
YouTube Data API v3 → Cuotas*).

### 9.2 Costs we use (units, regular bucket unless noted)

| Method | Cost | Notes |
|---|---|---|
| `videos.insert` | 1 (uploads bucket, 100/day) | per upload |
| `search.list` | 1 (search bucket, 100/day) | only reconcile fallback |
| `channels.list`, `videos.list`, `playlistItems.list`, `playlists.list`, `videoCategories.list`, `commentThreads.list`, `comments.list`, `i18nLanguages.list` | 1 per call/page | `videos.list` 1 unit for up to 50 ids (Brightbean `:58-61`) |
| `thumbnails.set`, `playlistItems.insert`, `commentThreads.insert`, `comments.insert`, `videos.update`, `videos.delete`, `comments.setModerationStatus` | 50 | writes (Google: "a write operation… usually costs 50 units"; Brightbean `:176-179`). Discovery's `part` description for comments says "The snippet part has a quota cost of 2 units" — legacy text, ignore. |
| `captions.insert` | 400 | not used |
| Analytics `reports.query` | separate API/quota | not counted against Data API |

"Every API request, including invalid requests, incurs a quota cost of at least one point" (official quota page,
paraphrase — UNVERIFIED exact wording).

Estimated daily use for one user with 2 channels: comments polling 96 polls × ~2 pages = ~200; analytics/posts ~20;
publishing (5 videos with thumbnail + playlist + polling) ~600 → **< 1,000 units/day**.

### 9.3 Reset and errors

- Daily quotas reset at **midnight Pacific Time** (official). Compute with `zoneinfo("America/Los_Angeles")`
  (Brightbean `google_errors.py:73-134`, fallback 08:00 UTC).
- Exhausted daily quota: **HTTP 403**, `error.errors[0].reason = "quotaExceeded"`, domain `youtube.quota` — the same
  for every bucket, so the bucket must be inferred from the URL (Brightbean `_quota_scope_for`, `youtube.py:95-114`).
  Also `dailyLimitExceeded` (legacy).
- Throttling: **403** `rateLimitExceeded` / `userRateLimitExceeded` (domain `usageLimits`) or **429** → short backoff
  (Brightbean 5-min cooldown `:63-66`; googleapiclient retries 429 and 403 with these two reasons, `http.py:80-147`).
- Channel upload cap: **400 `uploadLimitExceeded`** "The user has exceeded the number of videos they may upload" — a
  per-channel limit independent of project quota; no published number (varies by channel history/verification).
  → `RATE_LIMITED`, retry after 24 h, Spanish "Has alcanzado el límite diario de subidas de tu canal".
- Thumbnail rate: **429 `uploadRateLimitExceeded`**.

---

## 10. Analytics

### 10.1 Data API (lifetime counters)

- Channel: `channels.list?part=statistics&id=UC…` → `subscriberCount`, `viewCount`, `videoCount`, `commentCount`
  (legacy), `hiddenSubscriberCount` (strings, uint64). Since **2019-09-17** public subscriber counts are abbreviated:
  rounded **down to 3 significant figures** for channels > 1,000 subscribers, and the API returns the rounded number
  (e.g. `222000`) — exact counts only in YouTube Studio (Social Blade/RivalIQ/SEJ, secondary). UNVERIFIED whether an
  owner-authorised `mine=true` call is also rounded — assume yes. Use Analytics `subscribersGained/Lost` for exact
  daily deltas; label the absolute number "≈".
- Video: `videos.list?part=statistics,snippet,contentDetails,status&id=<≤50 ids>` → `viewCount`, `likeCount`,
  `commentCount`, `favoriteCount` (always 0, legacy), `dislikeCount` (private since Dec 2021; owner-only — UNVERIFIED
  that the owner still receives it). Omitted ids = deleted/private/not ours — never write zeros (Brightbean
  `:748-777`).
- Alternative `videos.batchGetStats` (own bucket, §9.1) — optional optimisation.

### 10.2 YouTube Analytics API v2

`GET https://youtubeanalytics.googleapis.com/v2/reports` (discovery `youtubeAnalytics.reports.query`):

| Param | Notes |
|---|---|
| `ids` | `channel==MINE` or `channel==UC…` (required) |
| `startDate`, `endDate` | `YYYY-MM-DD` (required), Pacific-time days |
| `metrics` | comma list (required) |
| `dimensions` | e.g. `day`, `month`, `video`, `creatorContentType` |
| `filters` | `video==id1,id2,…` (≤ **500** ids), multiple filters joined with `;` |
| `sort` | e.g. `day`, `-views` |
| `maxResults`, `startIndex` | pagination (1-based `startIndex`; Brightbean pages `dimensions=day` with `maxResults=200`, `:846-874`) |
| `currency`, `includeHistoricalChannelData` | not needed |

Metrics we use (names from Brightbean `:825-833`, OpenPost `analytics_video.go:138-148`, Postiz `:942-944`):
`views`, `engagedViews` (Shorts legacy definition, 2025), `estimatedMinutesWatched`, `averageViewDuration` (s),
`averageViewPercentage` (%, can exceed 100), `likes`, `dislikes`, `comments`, `shares`, `subscribersGained`,
`subscribersLost`.

Reports:
- **Daily channel series**: `ids=channel==MINE&dimensions=day&sort=day&startDate=D-7&endDate=D-1&metrics=…`.
- **Period totals** (never sum daily averages): same without `dimensions` (Brightbean audit rule: "Headline averages
  come from the whole-period report", README §Google).
- **Per video**: `dimensions=video&filters=video==id1,…&metrics=views,estimatedMinutesWatched,averageViewDuration,
  averageViewPercentage,likes,comments,shares,subscribersGained` (Brightbean `:953-1059`; some report docs require
  `sort` + `maxResults ≤ 200` for "top videos" queries without a video filter — UNVERIFIED for the filtered form).
- Responses: `columnHeaders[{name, columnType, dataType}]`, `rows[[…]]` — map by column name, keep reported zeros,
  treat missing rows as "no data" (Brightbean `:926-939`, OpenPost `:326-361`).
- **Latency**: ~48–72 h (Coupler/Airbyte/Improvado, secondary) → re-fetch and upsert the last 7 days every day.
- Low-traffic thresholds can hide some dimensions (secondary).
- Scope: `yt-analytics.readonly` (or `youtube.readonly`) per discovery (rev 20240707 and the Go copy rev 20230704).
  A third-party guide (rapidevelopers.com, May 2026) and a search snippet of the reports.query page claim requests
  "now require" `youtube.readonly` **in addition** — UNVERIFIED; harmless for us because we request both, but if the
  user unticks `youtube.readonly` on the granular consent screen, treat a 403 on `reports.query` as "missing
  `youtube.readonly`". Errors: 403 `forbidden`/`insufficientPermissions` → ask to
  reconnect with analytics permission; 400 for invalid metric/dimension combinations (PERMANENT; log the query).

### 10.3 Policy constraints on analytics (Developer Policies, 2026)

- Derived metrics policy: developers are "generally prohibited from creating metrics that replace or modify the data
  returned by the YouTube API Services"; custom scores/ratios (e.g. engagement rate) are allowed only for audited
  developers who apply for the Analytics & Reporting use case (amendments 2026-05-04 and 2026-06-01; policy III.L)
  (official derived-metrics policy + terms revision history, via search). Brightbean removed its YouTube
  engagement-rate card after the reviewer cited III.E.4.h (`youtube-audit-remediation.md:16-48`).
- Stored API data must be refreshed or deleted (the commonly cited 30-day rule; Brightbean refreshes copies by day 25
  and deletes by day 29, keeps statistics 180 days, `:91-103`). Fact-check: the terms revision history (2026-05-04 /
  2026-06-01, via search) confirms the 30-day refresh-and-delete rule still applies to most data; only **audited**
  clients whose quota-extension request was accepted may keep statistical-endpoint metrics (views, likes, subscriber and
  comment counts) and derived metrics for up to **36 months**. How strictly this applies to a personal local tool is
  still UNVERIFIED.
- Branding: attribute YouTube data with the "YouTube" name/icon next to it (III.F.2.a).

**Decisions:** (1) For YouTube rows, `engagement`/`interactions` (SPEC §12) are **not computed**; show native metrics
(views, watch time, avg view %, likes, comments, shares, subs gained/lost) with a small "YouTube" source label.
(2) Our SQLite snapshots are a local cache refreshed daily; on disconnect delete YouTube API data
(posts metrics, comments, snapshots) and revoke the token ("Desconectar y borrar datos de YouTube"). Low risk for a
personal unaudited tool, mandatory if the user ever applies for the audit.

### 10.4 Canonical keys (SPEC §12 mapping)

| SPEC key | YouTube source |
|---|---|
| `followers` | `statistics.subscriberCount` (rounded) |
| `posts_count` | `statistics.videoCount` |
| `views` (daily) | Analytics `views` (day) |
| new `watch_minutes` | Analytics `estimatedMinutesWatched` |
| new `subs_gained`, `subs_lost` | Analytics `subscribersGained/Lost` |
| `likes`, `comments`, `shares` (daily) | Analytics `likes`, `comments`, `shares` |
| post `views`, `likes`, `comments` | `videos.list statistics` |
| post `shares`, `avg_view_pct`, `avg_view_duration`, `watch_minutes` | Analytics per video |
| `reach`, `saves`, `profile_views` | not available on YouTube |

---

## 11. Comments

### 11.1 Read

`GET /youtube/v3/commentThreads?part=snippet,replies&allThreadsRelatedToChannelId=UC…&order=time&maxResults=100&textFormat=plainText[&pageToken=…]`

- Discovery rev 20260924 still lists `allThreadsRelatedToChannelId` ("Returns the comment threads of all videos of
  the channel and the channel comments as well"), `channelId` (channel comments only), `videoId`, `id`,
  `moderationStatus` (`published` default, `heldForReview`, `likelySpam`; owner only), `order` (`time` default,
  `relevance`), `searchTerms`, `textFormat` (`html` default, `plainText`), `maxResults` 1–100 (default 20). No
  deprecation notice found (channel "discussion" comments themselves were deprecated in 2024, so results are
  effectively video comments).
- Cost 1 unit per page. `replies.comments[]` contains only a subset of replies; when
  `snippet.totalReplyCount > len(replies)` fetch `comments.list?part=snippet&parentId=<top id>&maxResults=100`
  (Brightbean `:565-570`, `:619-636`).
- Early stop: threads sort by the top-level comment time, so a new reply can sit behind newer threads → Brightbean
  stops when a page's oldest thread is older than `since - 2 h` and caps at 5 pages (deep weekly sweep 50 pages)
  (`youtube.py:68-92`, `:593-615`). We copy the idea: poll every 15 min (SPEC §13), weekly deep sweep.
- Mapping to `CommentData`: `external_id` = comment id; `parent_external_id` = top-level id for replies;
  `object_external_id` = `snippet.videoId`; `object_permalink` = `https://www.youtube.com/watch?v=<vid>&lc=<comment id>`;
  `author_name` = `authorDisplayName`; `author_id` = `authorChannelId.value`; `text` = `textDisplay` (plain text;
  `textOriginal` is guaranteed only for the author); `created_time` = `publishedAt`; `from_us` =
  `authorChannelId.value == channel_id`. Also `likeCount`, `moderationStatus`, thread `canReply`, `isPublic`.
  New in discovery rev 20261012: `snippet.imageUrl` (read-only) — image/GIF attached to a comment, a signed URL that
  expires after **6 hours**; never store it as a permanent link (re-fetch, or show "imagen" placeholder).
- Comments may be disabled per video (`commentsDisabled` 403 on `videoId` queries), always disabled on made-for-kids
  videos.

### 11.2 Reply

`POST /youtube/v3/comments?part=snippet` body `{"snippet":{"parentId":"<top-level comment id>","textOriginal":"…"}}`
(50 units, force-ssl) — Brightbean `:642-664`. YouTube has one level of replies: to answer a reply, use its
`parentId` (the thread's top-level id). Check `canReply` first.

Errors (official `comments.insert` reference, via search): 400 `commentTextRequired`, `commentTextTooLong`,
`invalidCommentMetadata`, `operationNotSupported` (cannot reply to this thread; see `canReply`), `parentCommentIsPrivate`,
`parentIdMissing`, `processingFailure`; 403 `forbidden`, `ineligibleAccount` ("The YouTube account used to authorize
the API request must be merged with the user's Google account"); 404 `parentCommentNotFound`. Max text length:
UNVERIFIED (commonly 10,000 characters) — validate ≤ 10,000.

---

## 12. Error model

Envelope (all Google APIs): `{"error":{"code":403,"message":"…","status":"PERMISSION_DENIED","errors":[{"reason":
"quotaExceeded","domain":"youtube.quota","message":"…"}]}}` — read both `errors[].reason` and `status`; the Analytics
API leans on `status` (Brightbean `google_errors.py:1-55`). Strip HTML from `message` (quota message contains a link).

| HTTP | reason / status | Where | ErrorKind | Spanish message (proposal) |
|---|---|---|---|---|
| 401 | `authError`, `UNAUTHENTICATED`, "Invalid Credentials" | any | refresh once → `AUTH` | "La sesión de YouTube ha caducado; reconecta la cuenta." |
| 401 | `youtubeSignupRequired` | upload | `PERMANENT` | "Esta cuenta de Google no tiene canal de YouTube." |
| 403 | `insufficientPermissions` / `ACCESS_TOKEN_SCOPE_INSUFFICIENT` | any | `AUTH` | "Faltan permisos: reconecta y marca todas las casillas." |
| 403 | `quotaExceeded`, `dailyLimitExceeded` | any | `RATE_LIMITED` (`retry_after` = next Pacific midnight) | "Se agotó la cuota diaria de tu proyecto de Google; se reintentará a las 09:00 (hora de España)." |
| 403/429 | `rateLimitExceeded`, `userRateLimitExceeded`, 429 | any | `RATE_LIMITED` (5 min) | "YouTube pide ir más despacio; reintentando." |
| 403 | `accessNotConfigured` / "has not been used in project" | any | `PERMANENT` | "Activa la API en tu proyecto de Google Cloud." |
| 403 | `forbidden` | upload/thumbnail/comment | `PERMANENT` | context-specific |
| 403 | `forbiddenLicenseSetting`, `forbiddenPrivacySetting` | upload | `PERMANENT` | "YouTube no admite esa licencia/visibilidad para este vídeo." |
| 400 | `uploadLimitExceeded` | upload | `RATE_LIMITED` (24 h) | "Límite diario de subidas de tu canal alcanzado." |
| 400 | `invalidTitle`, `invalidDescription`, `invalidTags`, `invalidCategoryId`, `invalidPublishAt`, `invalidRecordingDetails`, `invalidVideoGameRating`, `invalidFilename`, `invalidVideoMetadata`, `defaultLanguageNotSet`, `mediaBodyRequired` | upload | `PERMANENT` | per field (Postiz `:99-179`) |
| 400 | `imageFormatUnsupported`, `imageTooTall`, `imageTooWide`, `failedPrecondition` | thumbnail | warning (video already live) | "Vídeo publicado, pero la miniatura no es válida." |
| 429 | `uploadRateLimitExceeded` | thumbnail | warning / retry later | |
| 403 | `commentsDisabled` | comments | `PERMANENT` | "Los comentarios están desactivados en este vídeo." |
| 404 | `videoNotFound`, `channelNotFound`, `parentCommentNotFound`, `playlistNotFound` | various | `PERMANENT` | |
| 5xx | `backendError`, `internalError` | any | `TRANSIENT` (resume upload from server `Range`) | |
| — | network error during final chunk | upload | `AMBIGUOUS` → reconcile | |
| 404/410 | (session URI) | upload | if before commit: restart session; after commit: §4.8 | |
| 400 | OAuth `invalid_grant` (+ `invalid_rapt`) | token | `AUTH` | §2.4 |

---

## 13. Composer: formats, options, validation (Spanish labels)

### 13.1 Formats

| key | label | media | rules |
|---|---|---|---|
| `video` | "Vídeo" | 1 video | ≤ 12 h and ≤ 256 GB; > 15 min requires `long_uploads_status == allowed` (else error "Tu canal solo permite vídeos de hasta 15 min; verifica tu canal en youtube.com/verify"); info if vertical/square and ≤ 3 min: "YouTube lo mostrará como Short" |
| `short` | "Short" | 1 video | duration ≤ 180 s (error); height ≥ width (error "Un Short debe ser vertical o cuadrado"); warning > 60 s about Content ID blocking; recommended 1080×1920 |

### 13.2 Options (keys are the contract for `rules.py` / provider)

| key | Label (ES) | Control / default | API mapping |
|---|---|---|---|
| `title` | "Título" | text, **obligatorio**, contador x/100, sin `<` `>`; prefill = primera línea del texto | `snippet.title` |
| (text) | "Descripción" — "Se usa el texto de la publicación" | contador de bytes x/5000; sin `<` `>` | `snippet.description` |
| `privacy_status` | "Visibilidad": **Pública** / **Oculto (no listado)** / **Privado** | radio; default Pública (audited) or Privado + banner (unaudited, API mode) | `status.privacyStatus` |
| `native_schedule` | "Programar en YouTube (se publica aunque el PC esté apagado)" | toggle; only Pública + programado ≥ 15 min + proyecto auditado; hint "Se sube ahora como privado y YouTube lo hará público a la hora indicada" | `status.privacyStatus=private` + `status.publishAt` |
| `category_id` | "Categoría" | select from `videoCategories.list?part=snippet&regionCode=<ES or channel country>&hl=es` (assignable only; OpenPost `:290-337`), cached per account; default "Gente y blogs" (22) | `snippet.categoryId` |
| `tags` | "Etiquetas" | chips; contador x/500 (comas y +2 por etiqueta con espacios) | `snippet.tags` |
| `made_for_kids` | "Audiencia: ¿Es contenido creado para niños?" → "Sí, es contenido creado para niños" / "No, no es contenido creado para niños" | radio **sin valor por defecto, obligatorio**; help: "Obligatorio por ley (COPPA). Si es para niños se desactivan comentarios y otras funciones." | `status.selfDeclaredMadeForKids` |
| `synthetic_media` | "Contenido alterado o sintético: ¿incluye contenido realista generado o modificado con IA?" Sí / No | radio, default **No** (shown explicitly) | `status.containsSyntheticMedia` |
| `paid_promotion` | "Incluye promoción pagada (product placement, patrocinio)" | checkbox, off | `paidProductPlacementDetails.hasPaidProductPlacement` (+ part) |
| `notify_subscribers` | "Notificar a los suscriptores" | checkbox, **on** | query `notifySubscribers` |
| `thumbnail_media_id` | "Miniatura personalizada (opcional)" | image from library; only `video`; help "Requiere canal verificado. JPG/PNG, 1280×720 recomendado" | `thumbnails.set` |
| `playlist_id` | "Añadir a lista de reproducción (opcional)" | select from `playlists.list?part=snippet&mine=true&maxResults=50` | `playlistItems.insert` |
| `first_comment` | "Primer comentario (opcional)" | textarea; disabled when made for kids | `commentThreads.insert` |
| `language` | "Idioma del vídeo" (avanzado) | select (es, en, …), default from channel | `snippet.defaultLanguage`, `defaultAudioLanguage` |
| `embeddable` | "Permitir insertar el vídeo en otras webs" (avanzado) | on | `status.embeddable` |
| `license` | "Licencia" (avanzado): "Licencia estándar de YouTube" / "Creative Commons - Atribución" | standard | `status.license` |

Banner when the project is unaudited and the *prueba de bloqueo* was not passed (§3.7): "Tu proyecto de Google no
está auditado por YouTube: los vídeos subidos con la API pueden quedar **privados y bloqueados**. Usa *Publicación
asistida* (recomendado), haz la prueba de bloqueo o solicita la auditoría." (Wording softened after the fact-check:
the lock is no longer certain, §3.1.)

Character counting: title with Python `len()` (YouTube's counting unit for emoji is UNVERIFIED — warn at 95+);
description with `len(text.encode("utf-8"))`; tags as described.

### 13.3 CSV (SPEC §14) additions

`youtube` (si/no), `formato_youtube` (video|short), `titulo_youtube`, `privacidad_youtube`
(publica|oculto|privado), `ninos_youtube` (si/no, obligatorio si youtube=si).

---

## 14. Provider design for Planificador de Redes (`providers/youtube.py`)

### 14.1 Settings / module functions

- `ProviderSettings` gains `google_client_id`, `google_client_secret` (encrypted), `youtube_project_audited: bool`,
  and `youtube_upload_lock: bool | None` (result of the *prueba de bloqueo*, §3.7; `None` = not tested). Automatic
  publishing is allowed when `youtube_project_audited or youtube_upload_lock is False`.
- Module functions (like TikTok in SPEC §6): `redirect_uri(settings)` →
  `f"{settings.base_url}/conexiones/callback/google"`; `new_pkce_pair()` → (verifier, S256 base64url challenge);
  `build_login_url(settings, redirect_uri, state, code_challenge)`; `exchange_code(...) -> GoogleTokens(access_token,
  expires_at, refresh_token, refresh_expires_at|None, scopes)`; `list_channels(http, access_token) -> list[YtChannel]`;
  `revoke(http, token)`.
- Connection route: callback → exchange → `list_channels` → if 1 channel create/update the account, if several show a
  chooser (same token for all is UNVERIFIED; store per channel) → check granted scopes and show missing-permission
  warnings.

### 14.2 `publish_step` state machine (`provider_state` keys)

```
phase: "init" | "uploading" | "finalizing" | "post" | "processing" | "done"
session_uri, session_started_at, total_bytes, uploaded_bytes, video_id, requested_privacy, returned_privacy,
thumbnail_applied, playlist_applied, first_comment_id, native_scheduled, poll_count
```

1. **init** (no `session_uri`, no `video_id`): refresh token if needed; validate (incl. `long_uploads_status`);
   build metadata; POST session; `save_state(session_uri, session_started_at, total_bytes, uploaded_bytes=0)` →
   `continue`.
2. **uploading**: if resuming after restart, status query first. Send 8 MiB chunks for at most ~60 s per call, then
   `save_state(uploaded_bytes)` and return `continue` (keeps the worker responsive). Before the **final** chunk call
   `ctx.before_commit(state)`. On `200/201`: `save_state(video_id, returned_privacy, phase="post")`.
   Session older than 6 days and no video → restart (new session) only if `commit_phase != commit_sent`.
3. **post**: thumbnail → playlist → first comment (flags; failures = warning events).
4. **processing**: `videos.list part=status,processingDetails`; `wait` per §8; lock check §3.4.
5. **done**: `StepResult("published", external_id=video_id, external_url=f"https://www.youtube.com/watch?v={id}",
   message=…)`; for `native_schedule` return `published` right after step 3 with `native_scheduled=True` (planner:
   "Programado en YouTube"), and schedule a check at `publishAt + 10 min`.

### 14.3 `reconcile(ctx)`

`video_id` in state → verify with `videos.list` (exists → `published`; empty → `unknown`). Else `session_uri` →
status query: 200/201 → `published`; 308 → `not_published`; 404/410 → §4.8 (title match → `published`, else
`unknown`). No session → `not_published`.

### 14.4 Other contract methods

- `refresh_credentials`: §2.4; returns `TokenUpdate(access_token, token_expires_at, refresh_token=new or None,
  refresh_expires_at from refresh_token_expires_in, scopes)`.
- `check_account`: `channels.list?part=id&mine=true` (1 unit); 401 after refresh → `AUTH`; channel id changed/missing
  → `AUTH` ("El canal ya no está disponible con esta autorización").
- `fetch_account_snapshot(day)`: Data API statistics (today) + Analytics daily rows for `day-7…day-1` (upsert all).
- `fetch_recent_posts(since)`: uploads playlist (`playlistItems.list part=contentDetails`, use
  `contentDetails.videoPublishedAt`) → `videos.list part=snippet,statistics,status,contentDetails` (50 ids/unit) →
  Analytics per-video for those ids; also auto-links assisted posts by title (§3.7). `thumbnail_url` =
  `snippet.thumbnails.medium.url`; `media_type` = `short`/`video` (our format if known; else heuristic: duration ≤ 180 s
  and vertical unknown → leave `video`).
- `fetch_comments(since)` / `reply_comment`: §11. `supports_comments = True` (needs force-ssl).
- `validate`: §13.

### 14.5 Assisted mode specifics

`auto_publish=False` default for YouTube when not audited. Modal (SPEC §11) additions: "Abrir YouTube Studio",
"Copiar título", "Copiar etiquetas", checklist of options, and "Pegar enlace del vídeo publicado (opcional)" — parse the
id from `watch?v=`, `youtu.be/`, `/shorts/` URLs to set `external_id` immediately.

---

## 15. Open questions and risks

1. **Is the private lock still enforced?** Google's reference (2026-10-08) and discovery rev 20261012 now say uploads
   from unverified projects are **not** restricted to private; the 2020 rule and the Help page say they are (§3.1).
   Run the *prueba de bloqueo* (§3.7) on day 1. If the lock is real, audit approval for a single-user self-hosted
   client is still unproven (no first-hand report) and API publishing stays private-only. (UNVERIFIED; highest product
   risk — but it may have just disappeared.)
2. **Lock visibility via API** — whether the insert response or later `videos.list` exposes the lock (privacy
   mismatch), and whether `publishAt` is ignored for unaudited projects. Test with a throw-away **unlisted** video on
   day 1 (a private test video cannot reveal a lock).
3. **Desktop client + non-root loopback path** (`/conexiones/callback/google`) — UNVERIFIED; Web client path is safe.
4. **"Publish app" prerequisites** — rclone says homepage + privacy URL now required even for personal apps; varies?
   (UNVERIFIED per account). Users without a site need the GitHub Pages trick or must live with Testing (weekly
   reconnect).
5. **Quota buckets** — some projects may not have been migrated to the 2026-06 bucket model (bundle.social);
   `videos.insert` cost may still be ~100 units of the 10k pool there. Read the error, don't assume.
6. **Thumbnail limit** — 50 MB is now confirmed (discovery revs 20260820–20261012 + revision history 2026-09-14);
   staying ≤ 2 MB is just extra safety, not a requirement.
7. **Uploads playlist includes private videos for the owner?** Needed for reconcile fallback and assisted-mode
   auto-linking of private/scheduled videos. (UNVERIFIED)
8. **Brand accounts**: one token = one channel (UNVERIFIED); test with a brand channel.
9. **Subscriber count rounding for the owner** (UNVERIFIED) — UI shows "≈".
10. **Developer Policies for local caches** (30-day refresh, derived metrics) — low enforcement risk for an unaudited
    personal tool, but any audit application would require the Brightbean-style compliance (refresh, delete on
    disconnect, no engagement rate).
11. **`made_for_kids` omitted** behaviour unknown — we never omit it.
12. Analytics enum values (`creatorContentType`), `engagedViews` availability per report, top-videos requirements —
    verify with a real channel.
13. The YouTube Studio upload deep link for assisted mode (`/videos/upload?d=ud`) is UNVERIFIED; fall back to the
    Studio home.
14. Granular consent: users who untick `force-ssl` lose comments (and playlists); UI must say which feature is missing.

---

## 16. Sources

Official Google (read via web-search snippets; not fetchable here):
- videos.insert — https://developers.google.com/youtube/v3/docs/videos/insert (1-unit "Video Uploads" bucket, 100
  calls/day, 256 GB, unverified-project notice)
- Videos resource — https://developers.google.com/youtube/v3/docs/videos
- Data API revision history — https://developers.google.com/youtube/v3/revision_history (2020-07-28 private lock;
  2024-10-30 `containsSyntheticMedia`; 2025-12-04 upload cost ~1600→~100; 2026-06-01 quota buckets; 2026-06-03
  `videos.batchGetStats`; 2026-09-14 thumbnail 2 MB→50 MB)
- Quota calculator — https://developers.google.com/youtube/v3/determine_quota_cost ; overview/getting started —
  https://developers.google.com/youtube/v3/getting-started
- Resumable uploads guide — https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- thumbnails.set — https://developers.google.com/youtube/v3/docs/thumbnails/set
- commentThreads.list — https://developers.google.com/youtube/v3/docs/commentThreads/list ; comments.insert —
  https://developers.google.com/youtube/v3/docs/comments/insert
- search.list — https://developers.google.com/youtube/v3/docs/search/list ; channels guide —
  https://developers.google.com/youtube/v3/guides/implementation/channels
- Made-for-kids guide — https://developers.google.com/youtube/v3/guides/made_for_kids_status
- Quota & compliance audits — https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits ; audit form
  — https://support.google.com/youtube/contact/yt_api_form
- Developer Policies — https://developers.google.com/youtube/terms/developer-policies ; derived metrics policy —
  https://developers.google.com/youtube/terms/derived-metrics-policy ; terms revision history —
  https://developers.google.com/youtube/terms/revision-history ; Required Minimum Functionality —
  https://developers.google.com/youtube/terms/required-minimum-functionality
- YouTube Analytics reports.query — https://developers.google.com/youtube/analytics/reference/reports/query ;
  Analytics revision history — https://developers.google.com/youtube/analytics/revision_history ; Shorts views notice
  — https://issuetracker.google.com/issues/406510753
- YouTube Help: Videos locked as private — https://support.google.com/youtube/answer/7300965 ; three-minute Shorts —
  https://support.google.com/youtube/answer/15424877 ; upload > 15 min — https://support.google.com/youtube/answer/71673
- Google OAuth: Manage OAuth Clients (redirect rules) — https://support.google.com/cloud/answer/15549257 ; Manage App
  Audience (Testing, 7 days, 100 users) — https://support.google.com/cloud/answer/15549945 ; verification exceptions —
  https://support.google.com/cloud/answer/13464323 ; production readiness —
  https://developers.google.com/identity/protocols/oauth2/production-readiness/overview ; loopback migration guide —
  https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration ; granular permissions —
  https://developers.google.com/identity/protocols/oauth2/resources/granular-permissions ; sensitive-scope
  verification — https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification

Machine-readable / code (downloaded and read in this session):
- `google-api-python-client` 2.201.0 wheel: `googleapiclient/discovery_cache/documents/youtube.v3.json` (rev
  20260924), `youtubeAnalytics.v2.json`, `googleapiclient/http.py`; 2.200.0 (rev 20260820) and 2.187.0 (rev 20250714)
  for diffs.
- `google-auth-oauthlib` 1.5.0 `google_auth_oauthlib/flow.py`.
- https://raw.githubusercontent.com/youtube/api-samples/master/python/upload_video.py
- https://raw.githubusercontent.com/porjo/youtubeuploader/master/README.md
- https://raw.githubusercontent.com/rclone/rclone/master/docs/content/drive.md (lines 2000-2073)
- npm `@rubytech/create-realagent-code@0.1.511` `payload/platform/plugins/google/references/auth.md` (empirical
  Testing token lifetime; third party)

Third-party (secondary; used only where marked): Blotato, bundle.social, outstand.so, postzen.dev, upload-post.com,
outlierkit.com (quota history); Social Blade, Rival IQ, Search Engine Journal (subscriber rounding); Coupler, Airbyte,
Improvado (Analytics latency); Ayrshare, upload-post (thumbnail verification); UiPath, Nango, Unified.to (refresh
token expiry causes); WorkOS (exact redirect match); Tubefilter, Entrepreneur (Shorts 3 min); MediaPost/SEJ (Shorts
view counting); AlternativeTo/creatorhandbook (Shorts custom thumbnails 2026); GitHub search snippets
(HeroesReplay #103/#250, fernandolisboa/bardo #92, gyuv/youtube-ai-agent #2) for publishAt behaviour on unaudited
projects.

Reference code (read-only, AGPL, nothing copied): paths and line numbers cited inline above.

---

## 17. Verificación (fact-check pass, 2026-10-15)

Method: each claim was re-checked against a source **different** from the one cited above. Main independent sources:
**GO** = `google-api-go-client` main `youtube/v3/youtube-api.json` (discovery rev **20261012**, newer than the
20260924 copy used above) and `youtubeanalytics/v2/youtubeanalytics-api.json`, plus `internal/gensupport/resumable.go`
and `googleapi/googleapi.go`; **NPM** = `@googleapis/youtube` 39.0.1 / 40.0.0 / 40.0.1 `build/v3.d.ts`; **RH** = scraped
copy of the Data API revision history (fetched 2026-09-08) in `mattmezstitchlab/byaime-one-page`
`research/sources/freshness-03-youtube-revision-2026.md`; **OSS** = other GitHub projects found by code search;
**WS** = web search over developers.google.com / support.google.com. "Secondary" = third-party only.

| # | Claim (section) | Verdict | Independent source |
|---|---|---|---|
| 1 | Unaudited projects created after 2020-07-28 have uploads locked private, "still in force Oct 2026" (§0, §3.1) | **CORRECTED** → conflicting | GO rev 20261012 + NPM 40.0.1 (2026-10-14) + Java/PHP clients: "Videos uploaded from unverified API projects are **not restricted** to private viewing mode"; live `videos.insert`/`videos` pages (LU 2026-10-08) quoted by OSS (`antonuemad/repeter`, `skalenetwork/agentpit-bench`, `23abdul23/YT`). Counter-evidence: RH 2020-07-28 entry never retracted (to 2026-09-01), Help 7300965. Enforcement now **UNVERIFIED**; added *prueba de bloqueo* (§3.7). |
| 2 | Omitted `privacyStatus` on insert (§4.3) | **CORRECTED** (added) | GO/NPM: defaults to `public`; some uploads default to `private` by creator account settings. |
| 3 | Testing status: test users ≤ 100, refresh token expires 7 days after consent (§1.4) | CONFIRMED (7 days official; 100 secondary) | WS: developers.google.com/health/setup ("time-based… expire after 7 days"), support 15549945; 100-user figure only in OSS (`skalenetwork`, `antonuemad`). |
| 4 | Unverified + In production: 100-user cap over the project's lifetime, cannot be reset (§1.4) | CONFIRMED | WS: support.google.com/cloud/answer/7454865 ("100 new users until it is verified"), answer/13463817 ("entire lifetime of the project… cannot be reset or changed"). |
| 5 | Web client accepts `http://localhost:<port>/<path>` (HTTPS exemption) (§1.3) | CONFIRMED | WS: support 15549257 ("Localhost URIs … are exempt"). |
| 6 | Desktop client: loopback `127.0.0.1` / `[::1]` / `localhost` on any port; non-root path (§1.3) | CONFIRMED forms / path **UNVERIFIED** | WS: loopback-migration guide lists the three forms with root path only. |
| 7 | `client_secret` required at token endpoint even for Desktop + PKCE (§1.3) | CONFIRMED (secondary) | WS: discuss.google.dev thread "Desktop OAuth PKCE exchange returns invalid_request … with no client secret"; ktaka.blog (2025) for Web clients. |
| 8 | PKCE `S256`, base64url **without padding**, verifier 43–128 chars (§2.2) | CONFIRMED | WS: Google installed-app guide (localized copy) "SHA-256 … base64url without padding", 43–128 chars; Google Java auth library `DefaultPKCEProvider` (S256, plain). |
| 9 | Resumable: `Location` session URI; chunks multiple of 256 KiB; `308` + `Range` (absent ⇒ nothing stored); status query `Content-Range: bytes */TOTAL`; `Retry-After` (§4.2–4.5) | CONFIRMED (+ note) | WS: YouTube resumable guide (en/de/pt via search); GO `resumable.go:74-125`, `googleapi.go:55-62` (256 KiB min). Note added: Go client uses `POST` + `X-GUploader-No-308: yes` (§4.4). |
| 10 | `videos.insert` maxSize 256 GiB, `video/*`, `notifySubscribers` default `true`, scopes (§4.1) | CONFIRMED | GO rev 20261012. |
| 11 | Title ≤ 100 chars, description ≤ 5000 bytes, no `<` `>`; tags ≤ 500 chars incl. commas (§4.3) | CONFIRMED | OSS `skalenetwork/agentpit-bench` `docs/youtube-shorts-spec.md:46-48` (quotes `videos` page LU 2026-10-08); WS adaptlypost quoting the reference; zh-tw reference for tag commas. |
| 12 | `publishAt` only with `private` (§7) | CONFIRMED | GO rev 20261012 description. "Never published before" and "past time ⇒ publishes now": secondary only (adaptlypost, singhamandeep) — keep as UNVERIFIED-ish, behaviour is harmless. |
| 13 | Quota: 2025-12-04 upload ~1600 → ~100 units; 2026-06-01 own buckets for `videos.insert` and `search.list`; 100 calls/day at 1 unit (§9.1) | CONFIRMED | RH entries 2025-12-04 and 2026-06-01 (verbatim); "Quota impact: 100 calls per day. 1 unit in the Video Uploads quota bucket" quoted from the live `videos.insert` page (LU 2026-10-08) by OSS `antonuemad/repeter`; WS bundle.social, postzen. |
| 14 | `videos.batchGetStats` has its own bucket (§9.1) | CONFIRMED + detail | RH 2026-06-03: 1 unit, own bucket, default **10,000 units/day** (added). |
| 15 | Exhausted quota = 403 `quotaExceeded` (domain `youtube.quota`); channel cap = 400 `uploadLimitExceeded` (§9.3) | CONFIRMED | Errors table in the RH scrape (`:1332`, "badRequest (400) uploadLimitExceeded … channel has exceeded the number of videos that it may upload"); OSS `Akshais97/sakhaa-forge` `docs/Project/Integrations/YouTube_integration.md`; WS ayrshare/bundle.social. |
| 16 | `uploadStatus`, `failureReason`, `rejectionReason`, `processingStatus`, `processingFailureReason`, `longUploadsStatus` enums (§8, §2.7) | CONFIRMED | GO rev 20261012 (identical lists). |
| 17 | Shorts: uploaded on/after 2024-10-15, square or vertical, ≤ 3 min ⇒ Short; no API flag; > 1 min + Content ID claim ⇒ blocked globally (§5) | CONFIRMED | WS: YouTube Help answer/15424877 (verbatim snippets); GO has no Shorts field. |
| 18 | `status.containsSyntheticMedia` added 2024-10-30 (§4.3) | CONFIRMED | RH `:395-414`. |
| 19 | `thumbnails.set` JPEG/PNG, 2 MB → 50 MB on 2026-09-14 (§6) | CONFIRMED | GO rev 20261012 `maxSize 52428800`; WS revision history snippet; OSS `MerlinStacks/socaliseit`. |
| 20 | Scopes: comment methods accept only `youtube.force-ssl`; `channels.list`/`playlistItems.insert` don't accept `youtube.upload` (§1.2) | CONFIRMED | GO rev 20261012. |
| 21 | `commentThreads.list?allThreadsRelatedToChannelId` still supported, `maxResults` 1–100 (default 20); reply via `comments.insert` with top-level `parentId`; errors incl. `operationNotSupported`, `parentCommentIsPrivate`, `ineligibleAccount` (§11) | CONFIRMED (+ new field) | GO rev 20261012; WS comments.insert reference; RH `:1447` for `ineligibleAccount`. Added `snippet.imageUrl` (6 h signed URL). |
| 22 | Analytics `filters=video==…` up to 500 IDs; `engagedViews` = old Shorts view definition (§10.2, §5) | CONFIRMED (+ update) | WS reports.query reference ("up to 500 IDs"); Analytics revision history (engagedViews available 2025-04-30). Added RH 2026-08-27: views counted from first frame for **all** formats. |
| 23 | `creatorContentType` values (§5) | UNVERIFIED (secondary) | CData connector docs: `LIVE_STREAM`, `SHORTS`, `STORY`, `VIDEO_ON_DEMAND`, `UNSPECIFIED`. |
| 24 | Analytics scopes `yt-analytics.readonly` **or** `youtube.readonly` (§1.2, §10.2) | CONFIRMED per discovery / conflicting claim UNVERIFIED | GO Analytics discovery (rev 20230704). Third-party claim that `youtube.readonly` is now additionally required — unconfirmed; harmless since we request both. |
| 25 | Subscriber counts rounded down to 3 significant figures in the API (§10.1) | CONFIRMED (secondary) | WS Tubefilter 2019-09-16, ppc.land, sociavault (2026). Owner-authorised rounding still UNVERIFIED. |
| 26 | Derived-metrics policy and 30-day storage rule (§10.3) | CONFIRMED (dates) | WS terms revision history: policies added 2026-05-04, clarified 2026-06-01; 36-month storage only for accepted audited clients. "Engagement rate" prohibition specifically: only via Brightbean's reviewer report — UNVERIFIED in the policy text. |
| 27 | Uploads playlist (`UU…`) lists the owner's private videos (§4.8) | UNVERIFIED | No source found either way. |

**Remaining uncertainties to test with a real project:** (1) lock enforcement (#1) — use the *prueba de bloqueo*;
(2) non-root path on a Desktop client (#6); (3) private videos in the uploads playlist (#27); (4) whether Analytics
`views` changed on 2026-08-27; (5) `creatorContentType` values; (6) owner-side subscriber rounding.

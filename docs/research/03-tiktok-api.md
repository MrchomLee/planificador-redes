# 03 — TikTok API (Login Kit + Content Posting API + Display API) for a LOCAL app

Research date: **2026-10-15**. Target: "Planificador de Redes", a single-user web app that runs on the
user's Windows PC at `http://localhost`, uses the user's **own** TikTok developer app (client key/secret
typed into Settings), and must connect a TikTok account, publish videos (and ideally photo carousels),
offer a "finish in the TikTok app" mode (Metricool's manual mode) and show basic analytics.

How to read this document

- **Ground truth** = three AGPL reference implementations, cloned and current to Oct 14–15 2026 (read only,
  nothing copied; we only describe behaviour):
  - `postiz-app` v1.47.0 (commit `021c2ee`, 2026-10-15): `libraries/nestjs-libraries/src/integrations/social/tiktok.provider.ts`
    (Content Posting API) and `tiktok.business.provider.ts` (TikTok **Business** API v1.3), DTO
    `libraries/nestjs-libraries/src/dtos/posts/providers-settings/tiktok.dto.ts`, UI
    `apps/frontend/src/components/new-launch/providers/tiktok/tiktok.provider.tsx`.
  - `brightbean-studio` (commit `dbe065f`, 2026-10-14): `providers/tiktok.py`,
    `apps/social_accounts/oauth_pkce.py`, `apps/social_accounts/oauth_aliases.py`, `README.md` §TikTok.
  - `openpost` (commit `dab6e02`, 2026-10-15): `apps/server/internal/platform/tiktok.go`,
    `analytics_video.go`, `postimport_tiktok.go`, docs `docs/reference/providers/tiktok.md`,
    `docs/research/provider-production-review-requirements-2026-09-19.md`.
- Official TikTok facts were checked through web-search snippets of developers.tiktok.com (the site cannot
  be fetched from this container). Pages used are listed in §13.
- **UNVERIFIED** = no primary source confirmed it, or sources disagree. Test these first with a real app.

---

## 0. Summary and decisions

| Topic | Fact (short) | Decision for Planificador de Redes |
|---|---|---|
| OAuth | Authorize `https://www.tiktok.com/v2/auth/authorize/`, token/refresh `POST https://open.tiktokapis.com/v2/oauth/token/` (form-encoded). Access token 24 h, refresh token 365 d, refresh may rotate. | Store both tokens + expiries in SQLite; refresh proactively (scheduler job + before every API call); persist the new `refresh_token` every time. |
| Redirect URI | **Web** platform: HTTPS only, absolute, static, max 10. **Desktop** platform: `http` allowed, host must be `localhost`/`127.0.0.1`, port required (wildcard `*` allowed), **PKCE mandatory** with a **hex** SHA-256 challenge. | Tell the user to enable the **Desktop** platform in their TikTok app and register `http://127.0.0.1:<port>/conexiones/callback/social1/` (no "tiktok" in the path). Always send PKCE (hex). Fallback: user-owned HTTPS relay/tunnel. |
| Audit | Unaudited clients: Direct Post is forced to `SELF_ONLY` (private), max 5 posting users / 24 h. Public Direct Post needs App Review **plus** a separate Content Posting audit, which reportedly rejects internal/single-user tools. | Design for **unaudited** operation: Direct Post as "Solo yo" (user flips visibility in the app) **or** "Enviar a la app de TikTok" (inbox draft). Keep the UI audit-compliant anyway. |
| Video upload | `FILE_UPLOAD` works from localhost (we PUT bytes to `upload_url`). Chunks 5–64 MB, last chunk up to 128 MB, `total_chunk_count = floor(size/chunk_size)`, 1–1000 chunks, sequential. | Implement chunked FILE_UPLOAD (streamed from disk). Never use PULL_FROM_URL for videos. |
| Photos | `/v2/post/publish/content/init/` accepts **only** `PULL_FROM_URL` from a **verified** domain/URL prefix (HTTPS, public). | Photo carousels are **not possible** from a pure localhost app. v1: TikTok = video only; photos only via an optional "public media host" setting (advanced) or manual mode. |
| Inbox mode | `POST /v2/post/publish/inbox/video/init/` (`video.upload`), user finishes in TikTok app; max 5 pending shares / 24 h. | Implement as "Terminar en la app de TikTok" = Metricool manual mode. Not affected by the SELF_ONLY restriction (third-party claim). |
| UX rules | Show creator nickname; privacy dropdown from `privacy_level_options` with **no default**; comment/duet/stitch unchecked by default, greyed out if disabled; commercial-content disclosure ("Your brand" / "Branded content"); consent line; max duration check. | Build the TikTok panel of the composer exactly to these rules (Spanish labels in §4.4). Fetch `creator_info` when the panel opens **and** again at publish time. |
| Status | Poll `POST /v2/post/publish/status/fetch/` (30 req/min/token): `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`, `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE`, `FAILED` (+`fail_reason`). Public post id (`publicaly_available_post_id`) only for **`PUBLIC_TO_EVERYONE`** posts and only after moderation; never for `FOLLOWER_OF_CREATOR`, `MUTUAL_FOLLOW_FRIENDS` or `SELF_ONLY` posts (corrected, see §14). | Persist `publish_id` immediately; publishing state machine with reconcile-only retries (never re-init after a successful init). |
| Analytics | `GET /v2/user/info/` (`user.info.stats`: follower/following/likes/video counts, lifetime only); `POST /v2/video/list/` + `/v2/video/query/` (`video.list`: view/like/comment/share counts, lifetime). No history, no reach/watch-time/demographics. | Daily snapshot job → our own time series. Analytics scopes optional (Display API availability for new apps is UNVERIFIED officially; two implementations say it is gone). **Per-post analytics need the public video id, which unaudited (`SELF_ONLY`) posts never get** → in practice only account-level analytics for unaudited users. |
| Scheduling | Content Posting API has no "publish_at" parameter. | Our in-process scheduler must be running (PC on) at publish time; say so in the UI. |

---

## 1. App setup: products, scopes, platforms

### 1.1 Products and scopes

| Scope | Comes with | Grants | Used by refs |
|---|---|---|---|
| `user.info.basic` | Login Kit (default) | `/v2/user/info/` fields `open_id`, `union_id`, `avatar_url`, `avatar_url_100`, `avatar_large_url`, `display_name` (only these six since the scope migration) | all three |
| `user.info.profile` | Display API / Scopes | `bio_description`, `profile_deep_link`, `is_verified`, `username` (+ `profile_web_link` per third parties) | Postiz, OpenPost (optional) |
| `user.info.stats` | Display API / Scopes | `follower_count`, `following_count`, `likes_count`, `video_count` | Postiz, OpenPost (optional) |
| `video.list` | Display API | `/v2/video/list/`, `/v2/video/query/` (user's **public** videos) | all three (optional in Brightbean/OpenPost) |
| `video.publish` | Content Posting API (Direct Post) | `/v2/post/publish/video/init/`, `/content/init/` with `DIRECT_POST`, `/creator_info/query/`, `/status/fetch/` | all three |
| `video.upload` | Content Posting API (Upload) | `/v2/post/publish/inbox/video/init/`, `/content/init/` with `MEDIA_UPLOAD` | all three |

Sources: TikTok scopes reference and user-info scope migration bulletin (§13); Postiz `tiktok.provider.ts:39-46`;
Brightbean `providers/tiktok.py:159-174` (requests only `user.info.basic,video.publish,video.upload` + optional
`video.list`, deliberately not `user.info.stats`, `:660-668`); OpenPost `tiktok.go:856-951` (capability sets:
minimal = `user.info.basic,video.publish,video.upload`; Display scopes optional).

- Scopes are sent comma-separated in `scope=`. Toggleable scopes can be **declined individually** by the user
  on the consent screen, so the token response's `scope` field must be checked (Postiz calls `checkScopes`,
  `tiktok.provider.ts:391`). → We store the granted scopes and enable/disable features per scope.
- **UNVERIFIED / conflicting:** OpenPost states "TikTok no longer offers the Display API to new apps"
  (`docs/reference/providers/tiktok.md:56`, `tiktok.go:187-189, 869-873`) and degrades gracefully without
  `user.info.profile`, `user.info.stats`, `video.list`. No official source found confirming this; the
  official Display API pages are still live (updated Aug 2026). A second, independent implementation says the
  same: TryPost (`trypostit/trypost`, `app/Http/Controllers/Auth/TikTokController.php` scopes() comment and
  `.env.example:137-138`, "Display API is no longer offered to new apps") and makes the scope list trimmable.
  → Our design must work with only the three publishing scopes and treat analytics scopes as an optional
  add-on (the scope list sent to TikTok must be configurable, otherwise the consent screen errors out).

### 1.2 Platforms (Web vs Desktop)

- An app registration selects one or more platforms: **Web, Desktop, Android, iOS**; Login Kit is available on
  all four. "Web and Desktop require the URL of your official website" (truncated snippet of
  *getting-started-create-an-app*). For apps created after 2024-09-09, all URLs in the configuration (ToS,
  Privacy Policy, Web/Desktop URL) must be **verified URL properties** before submitting for review.
- Which platform the redirect URI belongs to determines the redirect rules (§2.3).
- **UNVERIFIED:** whether a Desktop-only app can use the Content Posting API exactly like a Web app. Nothing
  found says otherwise and products are attached per app, but the only code-level hint is Brightbean's comment
  (`providers/tiktok.py:119-121` "PKCE … for desktop/native apps and localhost redirect URIs").
  *(Corrected in verification:)* OpenPost's `docs/reference/callback-urls.md:13` is a generic
  "local vs production" table repeated for every provider, not evidence that TikTok accepts it, and OpenPost
  sends no PKCE at all (`tiktok.go:79-87`), which the Desktop platform would require. Counter-evidence (all
  about the **Web** platform): Better Auth docs ("The TikTok API does not work with localhost … use NGROK"),
  django-allauth docs ("TikTok only support https callback url"), Auth.js provider notes ("Production
  applications cannot use localhost URLs"). No reference project actually ships a Desktop-platform TikTok flow.
  Must be tested on day 1, and the HTTPS-relay fallback (§2.6 step 5) must be designed, not just mentioned.

### 1.3 Redirect path gotcha

Brightbean renamed its callback slug from `tiktok` to `social1` because "TikTok's app review rejects redirect
URIs that contain the string 'tiktok'" (`apps/social_accounts/oauth_aliases.py:3-7`, README line 386 and
591). Not found in official docs, and **contradicted in practice** (verification): Postiz registers
`…/integrations/social/tiktok` (`tiktok.provider.ts:353`), OpenPost `…/api/v1/accounts/tiktok/callback`,
TryPost `${APP_URL}/accounts/tiktok/callback` (`.env.example:136`) and Auth.js documents
`…/api/auth/callback/tiktok`. So it is probably not a TikTok rule (maybe a one-off reviewer remark). Still
costless to follow: **our callback path will not contain "tiktok"** (`social1`).

---

## 2. OAuth 2.0 (v2)

### 2.1 Endpoints

| Step | Method + URL | Encoding |
|---|---|---|
| Authorize (browser) | `GET https://www.tiktok.com/v2/auth/authorize/` | query string |
| Code → token | `POST https://open.tiktokapis.com/v2/oauth/token/` | `application/x-www-form-urlencoded` |
| Refresh | same URL, `grant_type=refresh_token` | form |
| Revoke | `POST https://open.tiktokapis.com/v2/oauth/revoke/` with `client_key`, `token` (whether `client_secret` is also needed is UNVERIFIED; send `client_secret` too, it is harmless) | form |
| User info | `GET https://open.tiktokapis.com/v2/user/info/?fields=…` + `Authorization: Bearer <token>` | — |

Sources: Login Kit Web/Desktop docs, *oauth-user-access-token-management*; Postiz `tiktok.provider.ts:296-418`;
Brightbean `providers/tiktok.py:32-34, 188-251, 674-686` (revoke sends `client_key` + `token`; whether
`client_secret` is also required is UNVERIFIED); OpenPost `tiktok.go:18-31, 79-180`.

### 2.2 Authorize parameters

`client_key` (TikTok calls it *Client Key*, not client id), `response_type=code`, `scope` (comma list),
`redirect_uri` (must exactly match a registered one), `state` (CSRF; verify on return),
`code_challenge` + `code_challenge_method=S256` (PKCE), optional `disable_auto_auth` (`1` = always show
the consent page; Postiz Business uses it, `tiktok.business.provider.ts:373`, and TryPost sends it on the
standard v2 authorize URL, `TikTokController.php` connect(); useful to let the user pick another account).

Callback query: `code`, `scopes`, `state`, or `error` + `error_description`. The `code` must be URL-decoded
before the token call.

**PKCE specifics (important):** TikTok **deviates from RFC 7636**: `code_challenge = hex(sha256(code_verifier))`
(64 lowercase hex chars), **not** base64url. Verifier: 43–128 chars from the unreserved set, a new one per
authorization. Mandatory for Desktop; on Web it is accepted (Brightbean always sends it) but Postiz does
not send a challenge on Web and still works. Sources: Login Kit overview/desktop docs; Brightbean
`providers/tiktok.py:105-113`, `oauth_pkce.py:22-34` (verifier = `secrets.token_urlsafe(64)`).
**Verification note:** the hex encoding has only **one** code source (Brightbean, which cites
`login-kit-desktop`); no other implementation checked (Postiz, OpenPost, TryPost, Auth.js, Better Auth,
django-allauth, SocialiteProviders) sends a TikTok PKCE challenge at all, so it could not be cross-checked
in code, and the docs could not be re-read in this pass. → Make the challenge encoding a hidden setting
(`hex` default, `base64url` alternative) so a day-1 failure can be fixed without a code change; the error
appears at the authorize or token step, before any posting.

### 2.3 Redirect URI rules (official)

| Platform | Rules |
|---|---|
| **Web** | max 10 URIs, each < 512 chars, absolute, **must start with `https`**, static (no query params, no fragments). |
| **Desktop** | max 10 URIs, < 512 chars, absolute, `http` **or** `https`, static, **host must be `localhost` or `127.0.0.1`**, **port required**, wildcard port `*` supported (e.g. `http://127.0.0.1:*/callback/`). |

Community reports ("TikTok rejects localhost", allauth: "TikTok only supports https callback url, use ngrok")
refer to the **Web** platform. Exact-match is enforced on both authorize and token calls (Brightbean rebuilds
the redirect from the incoming request path to guarantee it, `oauth_aliases.py:40-52`). Postiz notes that
the TikTok **Business** portal only accepts redirect URLs ending in `/` (`tiktok.business.provider.ts:61-70`).

### 2.4 What the reference projects tell self-hosters

- **Postiz**: if `FRONTEND_URL` is not HTTPS it silently prefixes the redirect with the third-party bouncer
  `https://redirectmeto.com/` (`tiktok.provider.ts:348-354, 374-378`; same trick for Threads/VK/Slack). It
  sends `code_verifier=state` on token exchange but no challenge. Env vars `TIKTOK_CLIENT_ID/SECRET`
  (`.env.example:100-103`); README warns self-hosters must create their own apps and "TikTok can take weeks"
  to approve (`README.md:159`).
- **Brightbean**: register `{APP_URL}/social-accounts/callback/social1/`, add **Login Kit + Content Posting
  API**, scopes `user.info.basic, video.publish, video.upload, video.list`, record the review demo against a
  **Sandbox** (`README.md:587-602`).
- **OpenPost**: dev callback `http://localhost:8080/api/v1/accounts/tiktok/callback`, prod HTTPS
  (`docs/reference/callback-urls.md:13`; a generic table, same pattern for every provider, and OpenPost sends no
  PKCE, so this is **not** evidence that TikTok accepts it); Direct Post media requires a public `OPENPOST_MEDIA_URL`
  (`docs/reference/providers/tiktok.md:13, 38, 50`).
- **TryPost** (`trypostit/trypost`, Laravel; checked during verification): redirect
  `${APP_URL}/accounts/tiktok/callback`, `disable_auto_auth=1`, no PKCE, scopes configurable via
  `TIKTOK_SCOPES` (`.env.example:133-138`).

### 2.5 Token exchange and refresh

Body (form): `client_key`, `client_secret`, `code`, `grant_type=authorization_code`, `redirect_uri`,
`code_verifier` (PKCE). Refresh body: `client_key`, `client_secret`, `grant_type=refresh_token`,
`refresh_token`.

Response (fields per OpenPost `tiktok.go:143-153` and official docs): `access_token`, `expires_in` (**86400**
= 24 h), `refresh_token`, `refresh_expires_in` (**31536000** = 365 d), `open_id`, `scope`, `token_type`
(`Bearer`). Errors come back as top-level `error`, `error_description`, `log_id` (e.g. `invalid_grant`,
`invalid_client`, `invalid_request`). Access tokens can be refreshed without user interaction; **the refresh
may return a new refresh_token, which must replace the old one**. Postiz conservatively stores
`expiresIn = 23 h` (`tiktok.provider.ts:332, 414`).

`open_id` is per-app; `union_id` is stable across apps of the same developer. Postiz strips dashes from
`open_id` before storing (`:334`), others keep it verbatim — we keep it verbatim.

### 2.6 Recommended approach for Planificador de Redes

1. **Settings → Conexiones → TikTok** shows: Client Key, Client Secret fields and a read-only, copyable
   redirect URI: `http://127.0.0.1:<puerto>/conexiones/callback/social1/` and the `localhost` variant.
   Bind Uvicorn to `127.0.0.1` with a fixed port (configurable); register both hosts (or the wildcard-port
   form) in the TikTok app's **Desktop** platform config.
2. "Conectar TikTok": generate `state = secrets.token_urlsafe(32)` and a verifier (`secrets.token_urlsafe(64)`,
   ~86 chars, all unreserved); persist `{state, verifier, created_at}` (10-min TTL); redirect to authorize URL
   with `code_challenge = hashlib.sha256(verifier.encode()).hexdigest()`, `code_challenge_method=S256`,
   `disable_auto_auth=1`.
3. Callback: check `state`, handle `error`, exchange code (with verifier), compare granted `scope` with
   requested, fetch `/v2/user/info/?fields=open_id,union_id,avatar_url,display_name` (+`username` if
   `user.info.profile` granted, falling back like OpenPost `tiktok.go:182-201`), store account.
4. Token job: refresh when `expires_at - now < 1 h` and immediately before any publish; on `invalid_grant`
   mark the account "Reconexión necesaria" and notify.
5. **Fallback** if the Desktop platform cannot be used with Content Posting (UNVERIFIED §1.2): Web platform with
   an HTTPS redirect the user owns: (a) a static relay page on their own GitHub Pages/Cloudflare Pages that
   forwards `?code&state` to `http://127.0.0.1:<port>/…` via `location.replace`, or (b) a named
   Cloudflare Tunnel/ngrok fixed domain. Do **not** use a third-party bouncer like redirectmeto.com (leaks the
   code; also the redirect domain may need URL-property verification). The `redirect_uri` sent to the token
   endpoint must be the HTTPS one.

---

## 3. Sandbox, production review, Content Posting audit

| Stage | What it allows | Source |
|---|---|---|
| **Sandbox** | Up to **10 target users**, each an account you own (you log in with it while adding it and accept the Developer ToS). "Sandbox mode does not offer access to Content Posting API for **public** videos." | *add-a-sandbox* doc |
| **Production, unaudited** (passed App Review) | Any user can authorize, but Direct Post content is **restricted to private viewing**; `/publish/video/init/` returns `unaudited_client_can_only_post_to_private_accounts` for non-private attempts; **max 5 users can post in a 24 h window** (`reached_active_user_cap` for the 6th); `creator_info` typically returns `privacy_level_options: ["SELF_ONLY"]`. | Direct Post reference, Content Sharing Guidelines; Brightbean `tiktok.py:60-65, 356-371`; OpenPost review doc line 143 |
| **Audited** | Public Direct Post. | Content Sharing Guidelines |

**App Review (production)** requirements (official app-review guidelines as summarised by OpenPost
`docs/research/provider-production-review-requirements-2026-09-19.md:137-143`): a real, complete public
website (not a login page), visible ToS and Privacy Policy links, **verified URL properties** (web, ToS,
privacy, redirect, media URLs), explanation of every product/scope, **1–5 demo videos ≤ 50 MB each** showing
the full OAuth flow, every scope in use, the real UI, recorded on the submitted domain; first-time review
evidence must use **Sandbox**. Organisation ownership recommended but not required.

**Content Posting audit (separate)**: verifies the Direct Post UX against the Content Sharing Guidelines
(§4) and a broad public use case. OpenPost's research says "TikTok rejects internal or private
account-management utilities as a public Direct Post use case" (line 143); third-party blogs say the same
(**UNVERIFIED** officially). Postiz issue #1563 reports an audit rejection because the privacy dropdown was
pre-set to public (search result; issue not readable from here).

**Implication:** a single-user local tool realistically stays **unaudited**. Two supported ways to use it:

- **A. Sandbox app, user's own account as target user** (simplest; no website, no review). Expected to allow
  Direct Post `SELF_ONLY` and probably inbox upload (**UNVERIFIED**: inbox in sandbox; whether sandbox grants
  `user.info.stats`/`video.list` — Brightbean keeps analytics to `video.list` "so sandbox apps don't need
  the profile/stat scopes", `tiktok.py:171-175`).
- **B. Production app (after App Review)**: needs website + ToS/Privacy + demo video; still `SELF_ONLY` for
  Direct Post; inbox drafts can then be published publicly by the user from the TikTok app.

Some sources say the posting **account itself must be private** for unaudited clients (error name
`…can_only_post_to_private_accounts`; guideline text: "the account owner must first change their account
visibility to public, and then change the privacy settings of each content to 'Everyone'"). Treat as
**UNVERIFIED**; test with a private and a public account.

---

## 4. `creator_info` and the mandatory posting UX

### 4.1 Endpoint

`POST https://open.tiktokapis.com/v2/post/publish/creator_info/query/`, headers
`Authorization: Bearer <token>`, `Content-Type: application/json; charset=UTF-8`, empty body. Rate limit
**20 req/min per user token** (official). Response `data`:

| Field | Meaning |
|---|---|
| `creator_avatar_url` | avatar (confirmed in verification: TryPost `app/Services/Social/TikTokCreatorInfo.php` reads it) |
| `creator_username` | @handle (confirmed the same way) |
| `creator_nickname` | display name — **must be shown** on the posting screen |
| `privacy_level_options` | subset of `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR` (private accounts), `SELF_ONLY` |
| `comment_disabled`, `duet_disabled`, `stitch_disabled` | creator's app settings; if `true` the matching checkbox must be disabled/greyed |
| `max_video_post_duration_sec` | max duration this creator may post (all creators ≥ 180 s; some 300 s or 600 s) |

Sources: Query Creator Info reference; Media Transfer Guide; Brightbean `tiktok.py:280-297` and test fixture
`tests/providers/test_tiktok.py:376-381`; OpenPost `tiktok.go:617-675`; Postiz uses it only for max duration
(`tiktok.provider.ts:420-439`).

If `creator_info` returns an error such as `spam_risk_too_many_posts`, `spam_risk_user_banned_from_posting`
or `reached_active_user_cap`, the guidelines require stopping the publish attempt and asking the user to try
later. **Gotcha (TryPost `TikTokCreatorInfo.php`, verification):** `creator_info/query` reports these refusals
with **HTTP 200** and `error.code` set, so always read `error.code` whatever the HTTP status. TryPost groups
`spam_risk_too_many_posts`, `reached_active_user_cap`, `rate_limit_exceeded` as "limit reached, retry later"
and `spam_risk_user_banned_from_posting` as "cannot post".

### 4.2 Content Sharing Guidelines (what the audit checks)

From the official guidelines (snippets) and the compliant implementations they were matched against:

1. **Fetch fresh creator info** when rendering the "Post to TikTok" screen; show the **creator nickname** so
   the user knows which account receives the post.
2. **Duration**: block videos longer than `max_video_post_duration_sec`.
3. **Privacy**: a dropdown whose options are exactly `privacy_level_options`, **no default value** — the user
   must pick manually. (Postiz violates this: default `PUBLIC_TO_EVERYONE` in UI `tiktok.provider.tsx:137-150`
   and backend `tiktok.provider.ts:576-577`; Brightbean also defaults server-side `tiktok.py:36, 307-308`;
   OpenPost requires an explicit choice `tiktok.go:600-615`.)
4. **Interactions**: "Allow Comment", "Duet", "Stitch" checkboxes, **none checked by default**; disabled and
   greyed out when creator_info says so; Duet/Stitch not shown for photo posts.
5. **Commercial content disclosure**: a toggle, **off by default**. When on, two options:
   - **"Your brand"** (promoting yourself/own business) → API `brand_organic_toggle=true`; the post is
     labelled "Promotional content".
   - **"Branded content"** (promoting a third party) → `brand_content_toggle=true`; labelled
     "Paid partnership". Branded content **cannot be private**: `SELF_ONLY` must be shown but disabled when it
     is selected, and if `SELF_ONLY` was already chosen the privacy is cleared and the user is told why
     (confirmed in verification by a second implementation, TryPost `TikTokSettings.vue`, citing "TikTok UX
     Guideline Point 3b"; official wording not re-read).
   - Label shown to the user: "Paid partnership" whenever Branded content is on (alone **or** together with
     Your brand); "Promotional content" only for Your brand alone; plus "This cannot be changed once your
     video is posted" (TryPost `lang/en/posts.php:326-328`).
   - **Consequence for unaudited apps:** when `privacy_level_options == ["SELF_ONLY"]`, "Branded content"
     can never be posted; disable that option with an explanation.
   - With the toggle on, at least one option must be selected before posting.
6. **Consent line** before the publish button: "By posting, you agree to TikTok's **Music Usage
   Confirmation**" (link `https://www.tiktok.com/legal/page/global/music-usage-confirmation/en`), plus
   "and **Branded Content Policy**" (`https://www.tiktok.com/legal/page/global/bc-policy/en`) when Branded
   content is selected. Postiz renders this only when a disclosure option is on
   (`tiktok.provider.tsx:349-377`); compliant implementations show the Music line **always** (TryPost renders
   "By posting, you agree to TikTok's [Branded Content Policy] and [Music Usage Confirmation]" with the
   same two URLs, `app/Enums/SocialAccount/Platform.php:589-590`).
7. **Preview** of the exact media/caption being posted; the user must keep full control; the app must not add
   watermarks, logos, links or promotional text to the content.
8. After publishing, tell the user processing may take a few minutes before the post appears on the profile
   (TryPost: "After publishing, it may take a few minutes for the content to process and appear on your
   TikTok profile."; official wording not re-read).
9. **AI-generated content**: video `post_info.is_aigc` (bool) — expose as a checkbox "Contenido generado con IA"
   (Postiz `tiktok.provider.ts:587-593`, OpenPost `:314`). Not available for photo posts.

### 4.3 Unaudited apps: what to show

If `privacy_level_options == ["SELF_ONLY"]`, show the dropdown with only "Solo yo" (still no preselection) and
an info box: "Tu app de TikTok no está auditada: TikTok solo permite publicar en privado. Cambia la
visibilidad después en la app de TikTok, o usa 'Terminar en la app de TikTok'." (Brightbean's hint text,
`tiktok.py:60-65`, conveys the same.)

### 4.4 Proposed Spanish labels (TikTok panel of the composer)

| Element | Label |
|---|---|
| Account header | "Publicar como **{creator_nickname}**" + avatar |
| Mode | "Publicación directa" / "Terminar en la app de TikTok (borrador)" |
| Privacy (required, empty first option "Selecciona…") | `PUBLIC_TO_EVERYONE` "Todo el mundo", `MUTUAL_FOLLOW_FRIENDS` "Amigos", `FOLLOWER_OF_CREATOR` "Seguidores", `SELF_ONLY` "Solo yo" |
| Interactions | "Permitir comentarios", "Permitir Dúo", "Permitir Stitch" |
| Disclosure toggle | "Divulgar contenido comercial" + help "Indica que este contenido promociona bienes o servicios a cambio de algo de valor." |
| Options | "Tu marca" ("Promocionas tu propia marca o negocio. Se etiquetará como 'Contenido promocional'.") / "Contenido de marca" ("Promocionas a otra marca o a un tercero. Se etiquetará como 'Colaboración pagada'.") |
| AI | "Contenido generado con IA" |
| Consent | "Al publicar, aceptas la [Confirmación de uso de música] de TikTok" (+ " y la [Política de contenido de marca]") |
| Cover | "Fotograma de portada" (slider → `video_cover_timestamp_ms`) |

---

## 5. Direct Post — video

### 5.1 Init

`POST https://open.tiktokapis.com/v2/post/publish/video/init/` (`video.publish`), JSON body:

```
post_info:
  title                    caption incl. #hashtags/@mentions; max 2200 UTF-16 code units
                           (OpenPost tiktok.go:28, Brightbean max_caption_length 2200; Postiz caps at 2000)
  privacy_level            REQUIRED, one of creator_info.privacy_level_options
  disable_comment          bool
  disable_duet             bool
  disable_stitch           bool
  video_cover_timestamp_ms int (default first frame)
  brand_content_toggle     bool  ("Branded content")
  brand_organic_toggle     bool  ("Your brand")
  is_aigc                  bool  (AI-generated label)
source_info:
  source            "FILE_UPLOAD" | "PULL_FROM_URL"
  video_size        int bytes          (FILE_UPLOAD)
  chunk_size        int bytes          (FILE_UPLOAD)
  total_chunk_count int                (FILE_UPLOAD)
  video_url         https URL          (PULL_FROM_URL)
```

Response: `{"data":{"publish_id":"v_pub_file~…","upload_url":"https://open-upload.tiktokapis.com/…"},
"error":{"code":"ok","message":"","log_id":"…"}}` (`upload_url` only for FILE_UPLOAD; host name recalled,
not verified). Always check `error.code == "ok"`. Rate limit: **6 req/min per user token** (official, shared
wording on photo/upload pages).

Sources: Direct Post reference; Postiz `tiktok.provider.ts:562-624, 837-886`; Brightbean `tiktok.py:405-420,
483-524`; OpenPost `tiktok.go:305-321`. The caption-length 2200 is from the refs; the official video page was not
in the snippets (**UNVERIFIED**). Metricool's help claims the TikTok API "does not support line breaks" in
captions (**UNVERIFIED**; test).

### 5.2 FILE_UPLOAD chunking (Media Transfer Guide)

- Each chunk **≥ 5 MB and ≤ 64 MB**, except the **final chunk**, which may exceed `chunk_size` up to **128 MB**
  to absorb trailing bytes.
- `total_chunk_count = floor(video_size / chunk_size)` (the remainder is merged into the last chunk, not sent
  as an extra chunk). Official example: 50,000,123 bytes, chunk 10,000,000 → 5 chunks, last one 10,000,123.
- Videos **< 5 MB must be uploaded whole** (`chunk_size = video_size`, `total_chunk_count = 1`);
  videos **> 64 MB must be chunked**. Between 5 and 64 MB either is fine.
- **1–1000 chunks**, uploaded **sequentially**. (128 MB final-chunk cap and 1000-chunk cap cross-checked in
  verification against OpenPost's tests, `tiktok_test.go:498-577`, which encode the same Media Transfer
  Guide rules.)
- Ambiguity: MB = 10^6 or 2^20? Brightbean uses 64,000,000 decimal as the single-chunk limit (`tiktok.py:78-80`);
  Postiz/OpenPost use 64 MiB and a 5 MiB floor (`tiktok.provider.ts:715-732`, `tiktok.go:419-428`, test
  `tiktok_test.go:498-576`). Official whole-file example uses 4,194,304 bytes (< 5 MB) as chunk_size.

**Safe plan for us** (satisfies both interpretations):
`if size <= 64_000_000: chunk_size=size, n=1` else `chunk_size=10_000_000, n=size//chunk_size`; last chunk =
`size - (n-1)*chunk_size` (≤ ~20 MB). Max video ≈ 10 GB at 1000 chunks (well above TikTok's own size cap).

**PUT each chunk** to `upload_url` (no Authorization header needed — it is a presigned URL):

```
PUT {upload_url}
Content-Type: video/mp4 | video/quicktime | video/webm
Content-Length: {bytes in this chunk}
Content-Range: bytes {first_byte}-{last_byte}/{video_size}
```

Responses: `206 Partial Content` for intermediate chunks, `201 Created` when complete (Postiz accepts
200/201/206, `tiktok.provider.ts:797-800`). Stream from disk (Brightbean hit memory limits reading whole files,
`tiktok.py:504-508`). Postiz treats a network error during the PUT as **ambiguous** and goes on to poll status
instead of retrying the publish (`:897-907`). `upload_url` validity ~1 hour (recalled, **UNVERIFIED**).
Error `invalid_file_upload` (400) = declared sizes/chunks don't match (Postiz `:254-258`).

### 5.3 PULL_FROM_URL

`source_info.video_url` must be a **public HTTPS** URL under a **domain or URL prefix verified** in the
developer portal for this app (DNS TXT record or signature file / meta tag on the site; per-host, a CDN host
needs its own verification; no redirects). Otherwise `403 url_ownership_unverified`. Status goes through
`PROCESSING_DOWNLOAD`; failures `video_pull_failed`. Postiz moved videos **off** PULL_FROM_URL
("kept for when the TikTok PULL bug is fixed", `tiktok.provider.ts:626-711`); Brightbean prefers FILE_UPLOAD
because presigned S3/R2 URLs cannot be verified (`tiktok.py:323-333`). **Unusable from localhost.**
*(Verification note:)* not every reference avoids it: OpenPost Direct Post sends **PULL_FROM_URL** and uses
FILE_UPLOAD only for inbox uploads (`tiktok.go:241-260, 296-321`), and TryPost Direct Post also uses
PULL_FROM_URL (`app/Services/Social/TikTokPublisher.php` publishVideo()). Direct Post + FILE_UPLOAD is proven
in code only by Postiz and Brightbean; Postiz runs it in production, so it is still the right choice for us.

### 5.4 Video specs (Media Transfer Guide + third parties)

| Spec | Value | Confidence |
|---|---|---|
| Containers | MP4 (recommended), WebM, MOV | official |
| Codecs | H.264 (recommended), H.265, VP8, VP9 | official |
| Frame rate | 23–60 FPS | official |
| Resolution | min 360 px, max 4096 px per side | official min; max via Ayrshare |
| Duration | ≥ 3 s (Metricool/Ayrshare), ≤ `max_video_post_duration_sec` (180/300/600) | creator-specific max official; min UNVERIFIED |
| File size | 4 GB (recalled from the guide; also OpenPost `docs/reference/providers/index.md:85` "up to 10 minutes and 4 GB") vs 10 GB (Ayrshare) | not official — enforce 4 GB |

Matching `fail_reason` values: `file_format_check_failed`, `duration_check_failed`,
`frame_rate_check_failed`, `picture_size_check_failed` (Postiz `:129-283`).
→ Validate locally with ffprobe-equivalent metadata before init (container, codec, fps, w/h, duration vs
`max_video_post_duration_sec`, size).

### 5.5 Status polling

`POST https://open.tiktokapis.com/v2/post/publish/status/fetch/`, body `{"publish_id": "…"}`.
Limit **30 req/min per user token** (official). `data` fields: `status`, `fail_reason`,
`publicaly_available_post_id` (array; **sic**, the typo is the real field name — OpenPost also accepts the
correct spelling, `tiktok.go:749-757`), `uploaded_bytes`, `downloaded_bytes`(?).

| `status` | Meaning | Our state |
|---|---|---|
| `PROCESSING_UPLOAD` | FILE_UPLOAD bytes still being received/processed | publicando |
| `PROCESSING_DOWNLOAD` | PULL_FROM_URL download in progress | publicando |
| `SEND_TO_USER_INBOX` | inbox flow: notification sent to creator (also seen when a Direct Post is diverted to drafts, Brightbean `tiktok.py:93-101`) | "Pendiente en la app de TikTok" |
| `PUBLISH_COMPLETE` | posted (Direct) / creator finished posting (inbox) | publicado |
| `FAILED` | see `fail_reason` | error |

- The public post id appears only **after moderation** (usually ~1 min, sometimes hours, TryPost saw weeks) and
  only for posts that are **`PUBLIC_TO_EVERYONE`**. *(Corrected in verification:)* it is never reported for
  `FOLLOWER_OF_CREATOR`, `MUTUAL_FOLLOW_FRIENDS` or `SELF_ONLY` posts (TryPost `.ai/rules/tiktok.md`: "TikTok
  never reports an id for followers, friends or private posts"; OpenPost
  `docs/references/competitor-audit-2026-10-10.md:45`, citing the Get Post Status page updated 4 Aug 2026).
  Postiz only excludes `SELF_ONLY`/`MUTUAL_FOLLOW_FRIENDS` (`tiktok.provider.ts:1175-1182`), which is incomplete;
  OpenPost keeps the publish_id for SELF_ONLY (`tiktok.go:761-769`). TryPost keeps re-asking public posts every
  15 min for 30 days.
- **Never infer the video id from `video/list`** (caption or time matching): TryPost mapped 17 posts of a
  same-caption series to the wrong videos in Oct 2026 (`.ai/rules/tiktok.md`); OpenPost refuses too
  (`tiktok.go:759-760`). `/v2/video/query/` accepts only the numeric id (Brightbean `tiktok.py:573-611`).
  Post URL:
  `https://www.tiktok.com/@{username}/video/{id}` or `/photo/{id}` for photo posts (`publish_id` prefix `p_`)
  (Postiz `:520-526`).
- `publish_id` prefixes: `v_pub_file~`, `v_pub_url~`, `v_inbox_file~`, `v_inbox_url~`, `p_pub_url~`,
  `p_inbox_url~` (Postiz `:1155-1173`, Brightbean `:576-582`).
- The id is an int64: JS loses precision (Postiz regex workaround `:528-535`); **Python json is safe**, but
  store as TEXT.
- Polling cadence used by refs: Postiz every 20 s for ~9 min then durable re-checks (`:941-970`); Brightbean
  sweep every 60 s, unconfirmed timeout 6 h (README line 866); OpenPost reconcile after 30 s, 1 min, 15 min for
  inbox, 24 h window (30 days for inbox) (`tiktok.go:542-598`, `docs/development/platform-adapters.md:36`).
- **Never re-run init automatically once it returned a `publish_id`** — a duplicate on a live account cannot be
  undone (Brightbean README line 866; Postiz comments `:910-912`).
- Webhooks exist (post publish events) but need a public HTTPS callback → **not usable** locally; poll.
- Cancel (best effort): `POST /v2/post/publish/cancel/` with `publish_id` (path recalled, **UNVERIFIED**);
  failure reason `publish_cancelled`.

---

## 6. Upload to inbox ("Terminar en la app de TikTok") — Metricool manual mode

- `POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/` (`video.upload`), body **only**
  `source_info` (same FILE_UPLOAD/PULL_FROM_URL fields as §5). No `post_info`: caption, privacy, toggles are
  set by the user in the TikTok editor. (Postiz still sends `title` and warns that every other setting is
  discarded, `tiktok.dto.ts:61-70`, UI `tiktok.provider.tsx:62-65` says TikTok does not accept a title here →
  treat caption as **not transferable**; copy it to the clipboard for the user.) Verification: Postiz
  contradicts itself (DTO `:72-78` says the title is "the only setting TikTok keeps" on UPLOAD; UI `:62-65`
  says no title is accepted and the draft shows "#Postiz"); OpenPost sends **only** `source_info`
  (`tiktok.go:366-386`). Keep the clipboard approach; optionally test sending `post_info.title`.
- Then PUT chunks exactly as §5.2 (OpenPost `tiktok.go:376-417`). Status reaches `SEND_TO_USER_INBOX`; the
  creator gets an in-app notification, opens it, edits (can add trending music, effects — the reason
  Metricool offers manual mode) and posts; status later becomes `PUBLISH_COMPLETE`.
- Limits: **max 5 pending shares per 24 h** → `403 spam_risk_too_many_pending_share`; user's TikTok app too old →
  `app_version_check_failed` (Postiz `:136-142, 195-201`). How long a draft stays in the inbox: Postiz says 24 h,
  OpenPost waits 30 days (**UNVERIFIED**).
- Third-party sources say inbox drafts are **not subject to the unaudited SELF_ONLY restriction** nor the creator
  cap (**UNVERIFIED** officially, but consistent with the design: the creator chooses visibility in-app).
- Mapping to Metricool: Metricool's manual mode sends a phone/email notification that saves the video and
  copies the text; ours is better integrated: the video lands directly in the TikTok inbox at the scheduled
  time. Post shows "Enviado a TikTok — pendiente de completar en la app" (analogous to Metricool's "Enviado").

---

## 7. Photo posts

- `POST https://open.tiktokapis.com/v2/post/publish/content/init/`, body:
  `media_type: "PHOTO"`, `post_mode: "DIRECT_POST" | "MEDIA_UPLOAD"`,
  `post_info: {title (≤ 90 UTF-16), description (≤ 4000 UTF-16), privacy_level, disable_comment,
  auto_add_music, brand_content_toggle, brand_organic_toggle}` (DIRECT_POST; MEDIA_UPLOAD takes title/description
  only), `source_info: {source: "PULL_FROM_URL", photo_images: [≤ 35 URLs], photo_cover_index: 0-based}`.
  Same 6 req/min limit. Sources: Photo Post reference; OpenPost `tiktok.go:467-540`; Postiz `:814-835`.
- **`PULL_FROM_URL` is the only accepted source for photos** (official reference; Postiz `:820-821` "there is
  no FILE_UPLOAD path for photos"). URLs must be public and **verified** by the app.
- Formats: JPEG and WebP (no PNG) — OpenPost `tiktok.go:838-845`, bundle.social; ≤ 20 MB each (OpenPost doc
  line 35; UNVERIFIED officially); Postiz observed `picture_size_check_failed` for images whose **shorter side >
  1080 px** and pre-validates that (`tiktok.provider.ts:73-87`); TryPost instead treats **width > 1080 px** as
  the trigger and re-hosts a resized JPEG (`TikTokPublisher.php` resolvePhotoUrl(), verification). Safe rule:
  scale so that width ≤ 1080 **and** shorter side ≤ 1080. Duet/stitch/is_aigc don't exist for photos.
- **Implications for a localhost app**: impossible without a public HTTPS host on a verified domain. Options:
  1. v1: TikTok supports **video only**; photo carousels are disabled for TikTok with an explanation.
  2. Advanced setting "Servidor público de medios": user supplies an HTTPS base URL on their own domain
     (Cloudflare R2/S3 public bucket with custom domain, or a named Cloudflare Tunnel exposing only
     `/media-publica/<token>/…` of our app), verified as a URL property in their TikTok app. The app uploads the
     images there (or serves them via the tunnel) for the duration of the pull. Convert PNG → JPEG and resize so
     the shorter side ≤ 1080 px.
  3. Manual mode: notify the user and open the folder with the images (like Metricool manual).
  Also: an alternative "slideshow" is to render the images to an MP4 locally and post it as a video
  (product decision; loses the native carousel).

---

## 8. Rate limits and error codes

### 8.1 Rate limits (per user access token, 1-minute sliding window)

| Endpoint | Limit | Confidence |
|---|---|---|
| `/v2/post/publish/video/init/`, `/inbox/video/init/`, `/content/init/` | 6 / min | official |
| `/v2/post/publish/creator_info/query/` | 20 / min | official |
| `/v2/post/publish/status/fetch/` | 30 / min | official (search snippet) |
| Display API (`/user/info/`, `/video/list/`, `/video/query/`) | 600 / min per endpoint | third-party (Phyllo) — UNVERIFIED |
| Daily posts per creator via API | "typically around 15 posts per day / creator account" (guidelines snippet); third parties say 15–25 | partially official; enforce 15/day locally |
| Inbox pending shares | 5 per 24 h | official |
| Unaudited client posting users | 5 per 24 h | official |

Exceeding → HTTP 429 `rate_limit_exceeded`. Brightbean configures itself conservatively at 200 req/h, 5000/day,
**5 publishes/day** (`tiktok.py:177-182`; its own choice, not official).

### 8.2 Response envelope

All non-OAuth v2 endpoints: `{"data": {...}, "error": {"code": "ok" | "<code>", "message": "...", "log_id": "..."}}`.
Success is `error.code == "ok"` (OpenPost `tiktok.go:953-979`). OAuth endpoints use top-level `error` /
`error_description`. (The Business API v1.3 instead returns HTTP 200 with `{"code": 0|n, "message", "data"}`
and auth header `Access-Token`, Postiz `tiktok.business.provider.ts:112-311`.)

### 8.3 Error codes to handle

| HTTP | `error.code` / `fail_reason` | Meaning | Action |
|---|---|---|---|
| 401 | `access_token_invalid` | expired/revoked token | refresh once, else "Reconectar" |
| 401/403 | `scope_not_authorized`, `scope_permission_missed` | user declined scope / app lacks it | permanent; reconnect / enable product |
| 403 | `unaudited_client_can_only_post_to_private_accounts` | non-private Direct Post from unaudited app | permanent; suggest "Solo yo" or inbox |
| 400 | `privacy_level_option_mismatch` | privacy missing/not in options | permanent; re-query creator_info |
| 400 | `invalid_param` / `invalid_params` | bad body (Content Posting uses the singular; Display API `video/list` answers an expired cursor with `invalid_params`, TryPost `TikTokPublicationCollector.php:42`; Postiz matches the plural) | permanent (for `video/list`: restart paging without cursor) |
| 4xx | `invalid_publish_id`, `token_not_authorized_for_specified_publish_id` | status fetch for an unknown publish_id or one created with another token (TryPost `TikTokPublishException.php:76`) | stop polling; manual check |
| 400 | `invalid_file_upload` | chunk/size mismatch | permanent (bug) |
| 403 | `url_ownership_unverified` | PULL_FROM_URL domain not verified | permanent |
| 403 | `spam_risk_too_many_posts` | daily post cap for this user | retry tomorrow |
| 403 | `spam_risk_too_many_pending_share` | 5 inbox drafts pending | ask user to finish drafts |
| 403 | `spam_risk_user_banned_from_posting` | account banned from posting | permanent |
| 403 | `spam_risk_text`, `spam_risk` | caption/content flagged | permanent; edit |
| 403 | `reached_active_user_cap` | unaudited app's 5-users/24 h quota | retry tomorrow |
| — | `app_version_check_failed` | user's TikTok app outdated (inbox) | ask to update app |
| 429 | `rate_limit_exceeded` | rate limit | backoff + retry (safe only **before** init succeeded) |
| 5xx | `internal_error`, `server_error`, `temporarily_unavailable` | TikTok side | retry with backoff (not after init) |
| status `FAILED` | `file_format_check_failed`, `duration_check_failed`, `frame_rate_check_failed`, `picture_size_check_failed`, `video_pull_failed`, `photo_pull_failed`, `publish_cancelled`, `auth_removed`, `internal`, and also `spam_risk_too_many_posts`, `spam_risk_user_banned_from_posting`, `spam_risk_text`, `spam_risk` as `fail_reason` values (TryPost `TikTokPublishException.php:92-109`) | processing failure | show reason in Spanish |

Note (TryPost `TikTokPublishException.php:124-147`): TikTok answers **401** for `scope_not_authorized` /
`scope_permission_missed` too, so a bare 401 on a posting endpoint is **not** proof of a dead token; only
`error.code == access_token_invalid` (or a 401 on `/v2/user/info/`) means "reconnect".

Sources: Direct Post / Upload / Photo references (search snippets), Get Post Status reference (fail_reason table
not retrieved — values above are from Postiz `tiktok.provider.ts:91-294`, `tiktok.business.provider.ts:112-269`
and Brightbean `tiktok.py:46-58` "permanent" set; **UNVERIFIED** as a complete list).

---

## 9. Analytics (Display API)

### 9.1 Account

`GET https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,avatar_url,follower_count,following_count,likes_count,video_count`
(`user.info.stats` for the four counts). **Lifetime totals only, no date range** (Brightbean
`account_metrics_supports_date_range = False`, `tiktok.py:154-157`). Postiz `analytics()` shows these as
single-day values (`tiktok.provider.ts:989-1036`); OpenPost `analytics_video.go:34-35`.

### 9.2 Videos

- **List**: `POST https://open.tiktokapis.com/v2/video/list/?fields=<csv>`, JSON body `{"max_count": 20, "cursor": <int>}`
  (max 20 per page; `cursor` = UTC unix ms; `data.videos[]`, `data.cursor`, `data.has_more`), sorted by
  `create_time` desc. Returns the user's **public** videos (scope description) — private `SELF_ONLY` posts are
  expected **not** to appear (**UNVERIFIED**; matters a lot for unaudited apps).
- **Query**: `POST https://open.tiktokapis.com/v2/video/query/?fields=<csv>`, body
  `{"filters": {"video_ids": [≤ 20 ids]}}`; verifies the videos belong to the user; also used to **refresh the
  expiring `cover_image_url`**.
- Video fields (both endpoints): `id`, `create_time` (unix s), `cover_image_url` (TTL-limited), `share_url`
  (strip query params — they include the client key, Postiz `:1233-1236`), `video_description`, `duration`,
  `height`, `width`, `title`, `embed_html`, `embed_link`, `like_count`, `comment_count`, `share_count`,
  `view_count`. Sources: v2 video query/list references; OpenPost `tiktok.go:29`, `postimport_tiktok.go:17, 47`;
  Postiz `:1039-1111, 1249-1304`; Brightbean `:613-658`.
- Counts are **lifetime**; no reach, impressions, watch time, completion rate, traffic sources or demographics
  through this API (Metricool shows more for TikTok — likely via TikTok's business/partner APIs; out of scope).
- To map our post → TikTok video id: `publicaly_available_post_id` from status fetch (§5.5); Brightbean resolves
  publish handles lazily at analytics time (`tiktok.py:573-611`). For inbox posts there is no reliable link
  unless status reaches `PUBLISH_COMPLETE` with an id; OpenPost refuses to match by caption/time
  (`tiktok.go:759-760`), and TryPost documents a real mis-match incident when it tried.
- **Consequence for unaudited users (verification):** Direct Post can only be `SELF_ONLY`, and TikTok gives a
  public id only to `PUBLIC_TO_EVERYONE` posts, so **our own posts cannot be linked to per-video metrics**.
  Whether status fetch starts returning the id after the user flips the video to public in the app is
  UNVERIFIED (TryPost keeps asking for 30 days, which suggests it might). UI: per-post metrics show "No
  disponible (publicación privada)"; the analytics tab shows account totals and the public videos list.

### 9.3 Design

Daily job (and "Actualizar" button): snapshot account counts into `account_metric_snapshots(date, followers, following, likes, videos)`,
page through `video/list` (stop when `create_time` < last sync and no tracked post is missing), upsert
`post_metric_snapshots(video_id, date, views, likes, comments, shares)`, download thumbnails locally (cover URLs
expire). Charts = deltas between snapshots. Hide the analytics tab with an explanation if scopes are missing.

---

## 10. TikTok Business API (alternative seen in Postiz) — not recommended

Postiz added a second provider on `https://business-api.tiktok.com/open_api/v1.3` (`tiktok.business.provider.ts:59`):
authorize on the same `www.tiktok.com/v2/auth/authorize/`, token at `/tt_user/oauth2/token/` (JSON body with
`client_id`, `client_secret`, `auth_code`, `redirect_uri`), `Access-Token` header, `/business/video/publish/` and
`/business/photo/publish/` (**PULL_FROM_URL only**, verified domain), `/business/publish/status/`, music library
and location tags; video posts have **no privacy field**. Requires a TikTok Business account and a public verified
media domain → incompatible with a localhost-only app. Postiz uses `MIGRATE_PROVIDERS="tiktok:tiktok-business"` to
move users (`.env.example:212-213`).

---

## 11. Concrete implications for Planificador de Redes

1. **Connection wizard (Spanish, step by step):** create app at developers.tiktok.com → add **Login Kit** and
   **Content Posting API** (+ optional Display API/"Scopes": `user.info.profile`, `user.info.stats`, `video.list`)
   → enable **Desktop** platform → register our redirect(s) → create a **Sandbox** and add their own account as
   **target user** → paste Client Key/Secret → "Conectar". Explain the private-only limitation upfront.
2. **Data model:** `tiktok_accounts(open_id, union_id, username, display_name, avatar_path, access_token,
   access_expires_at, refresh_token, refresh_expires_at, granted_scopes, app_audited_guess)`;
   `tiktok_publish_jobs(post_id, mode[direct|inbox], publish_id, status, fail_reason, public_post_id,
   bytes_sent, created_at, last_checked_at)`. Encrypt tokens at rest (DPAPI on Windows or a local key).
3. **Composer TikTok panel** built per §4 with live `creator_info` (HTMX request when the panel opens); validation
   before scheduling: exactly one video (or photos only if public host configured), duration ≤ max, privacy chosen,
   disclosure consistency, caption ≤ 2200 UTF-16 units (count with `len(s.encode('utf-16-le'))//2`).
4. **Publisher:** refresh token → re-query creator_info (privacy still allowed? duration?) → init (6/min) →
   persist `publish_id` **before** uploading → stream chunks → poll status (≤ 30/min; 5 s, 10 s, 20 s, then every
   60 s up to 30 min, then hourly reconcile up to 24 h; inbox: up to 30 days at low frequency) → resolve public id.
   Retry only pre-init failures.
5. **Scheduler:** TikTok has no server-side scheduling → posts publish only while the app runs; on startup run a
   catch-up for overdue posts and ask before publishing late ones.
6. **Media library:** keep originals; generate TikTok-compliant derivatives if needed (fps 23–60, ≥ 360 px,
   MP4/H.264) — optional ffmpeg dependency (UNVERIFIED availability on the user's PC; make it optional).
7. **Preview:** vertical 9:16 mock with caption + username (OpenPost `packages/social-preview/src/TikTokPreview.svelte`
   for inspiration only).

---

## 12. Open questions and risks

1. **Desktop platform + Content Posting API**: does a Desktop-platform redirect (`http://127.0.0.1:port`) work for
   an app that uses the Content Posting API, and does the Desktop platform demand a verified "Desktop URL" even
   in sandbox? (UNVERIFIED — test first; fallback = HTTPS relay.)
2. **Sandbox capabilities**: does a sandbox client get `video.upload` (inbox), `user.info.stats` and `video.list`?
   Any expiry of sandbox apps/tokens? (UNVERIFIED)
3. **Private account requirement** for unaudited Direct Post (`…can_only_post_to_private_accounts`). (UNVERIFIED)
4. **Display API for new apps** (OpenPost says no longer offered). If true, new users get no follower counts and no
   per-video stats, only publish status. (UNVERIFIED)
5. `video.list` probably excludes `SELF_ONLY` videos (UNVERIFIED), and status fetch never returns a public id for
   non-`PUBLIC_TO_EVERYONE` posts (verified in two refs) → unaudited users get no per-post analytics for posts
   made through our app. Open: does the id appear after the user makes the video public in the app?
6. Max video file size (4 GB vs 10 GB), min duration (3 s), `upload_url` lifetime, caption line breaks, exact
   fail_reason list, revoke parameters, cancel endpoint path. (UNVERIFIED)
7. Inbox draft lifetime (24 h per Postiz vs 30 days per OpenPost). (UNVERIFIED)
8. "No 'tiktok' in redirect path" rule (Brightbean only; contradicted by Postiz, OpenPost, TryPost, Auth.js). Cheap
   to comply anyway.
9. Audit rejection for single-user tools means public Direct Post will likely **never** be available to our users;
   UX must make "Solo yo + cambiar en la app" and "Terminar en la app" first-class, not error states.
10. Photo carousels need a public verified HTTPS host → out of scope for a pure local v1.
11. TikTok policy churn: limits and audit rules change often; keep all limits in a config table, surface raw
    `error.code` + `log_id` in the UI "Detalles técnicos" for support.
12. **PKCE encoding** (hex vs base64url): single code source (Brightbean). Keep it switchable; test on day 1
    together with item 1.

---

## 13. Sources

Official (via search snippets; not fetchable from this container):
- Login Kit overview — https://developers.tiktok.com/docs/en/login-kit-overview
- Login Kit for Web — https://developers.tiktok.com/docs/en/login-kit-web
- Login Kit for Desktop — https://developers.tiktok.com/docs/en/login-kit-desktop
- Manage user access tokens — https://developers.tiktok.com/docs/en/oauth-user-access-token-management
- Scopes reference — https://developers.tiktok.com/docs/en/tiktok-api-scopes
- User Info scope migration — https://developers.tiktok.com/bulletin/user-info-scope-migration
- Get user info v2 — https://developers.tiktok.com/docs/en/tiktok-api-v2-get-user-info
- Video list v2 — https://developers.tiktok.com/docs/en/tiktok-api-v2-video-list
- Video query v2 — https://developers.tiktok.com/docs/en/tiktok-api-v2-video-query
- Display API overview / get started — https://developers.tiktok.com/docs/en/display-api-overview , https://developers.tiktok.com/docs/en/display-api-get-started
- Content Posting get started (Direct Post / Upload) — https://developers.tiktok.com/docs/en/content-posting-api-get-started , https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content
- Direct Post reference — https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post
- Upload (inbox) reference — https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video
- Photo post reference — https://developers.tiktok.com/docs/en/content-posting-api-reference-photo-post
- Query creator info — https://developers.tiktok.com/docs/en/content-posting-api-reference-query-creator-info
- Get post status — https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status
- Media Transfer Guide — https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide
- Content Sharing Guidelines — https://developers.tiktok.com/docs/en/content-sharing-guidelines
- Add a sandbox — https://developers.tiktok.com/docs/en/add-a-sandbox
- Create an app — https://developers.tiktok.com/docs/en/getting-started-create-an-app
- App review guidelines — https://developers.tiktok.com/docs/en/app-review-guidelines
- Music Usage Confirmation — https://www.tiktok.com/legal/page/global/music-usage-confirmation/en
- Branded Content Policy — https://www.tiktok.com/legal/page/global/bc-policy/en

Third-party (secondary; used only where marked):
- Metricool help "Schedule and post on TikTok" — https://help.metricool.com/fr/schedule-and-post-on-tiktok-bkepi (and EN troubleshooting https://help.metricool.com/en/article/tiktok-publishing-and-upload-troubleshooting-guide-1umbvdy/)
- Postiz issue #1563 (audit rejection, default privacy) — https://github.com/gitroomhq/postiz-app/issues/1563
- voholabs-studio PR #59 (guideline-compliant post screen) — https://github.com/voholabs/voholabs-studio/pull/59
- Phyllo rate limits 2026 — https://www.getphyllo.com/post/tiktok-api-rate-limits-in-2026-quotas-errors-workarounds
- AdaptlyPost chunk-size and domain-verification notes — https://adaptlypost.com/blog/tiktok-api-chunk-size-64mb-limit , https://adaptlypost.com/blog/tiktok-api-pull-from-url-domain-verification
- Ayrshare TikTok media guidelines — https://www.ayrshare.com/docs/media-guidelines/tiktok
- bundle.social photo posting — https://bundle.social/blog/tiktok-photo-posting-api
- django-allauth TikTok provider notes — https://docs.allauth.org/en/dev/socialaccount/providers/tiktok.html
- PostPeer / PostZen / Outstand audit blogs — https://www.postpeer.dev/blog/best-tiktok-posting-api , https://www.postzen.dev/blog/tiktok-api , https://www.outstand.so/blog/tiktok-content-posting-api

Reference code (read-only, AGPL, nothing copied): paths and line numbers cited inline above.

Added during verification (read-only, nothing copied; shallow clones from github.com):
- TryPost — https://github.com/trypostit/trypost (`app/Services/Social/TikTokPublisher.php`,
  `TikTokCreatorInfo.php`, `app/Exceptions/Social/TikTokPublishException.php`, `app/Enums/TikTok/PublishStatus.php`,
  `resources/js/components/posts/editor/TikTokSettings.vue`, `lang/en/posts.php`, `.ai/rules/tiktok.md`,
  `app/Services/Analytics/Collectors/*/TikTok*.php`, `.env.example`)
- django-allauth TikTok provider + docs — https://github.com/pennersr/django-allauth (`allauth/socialaccount/providers/tiktok/`)
- Auth.js TikTok provider — https://github.com/nextauthjs/next-auth (`packages/core/src/providers/tiktok.ts`)
- Better Auth TikTok provider + docs — https://github.com/better-auth/better-auth (`packages/core/src/social-providers/tiktok.ts`, `docs/content/docs/authentication/tiktok.mdx`)
- SocialiteProviders TikTok — https://github.com/SocialiteProviders/Providers (`src/TikTok/Provider.php`)

---

## 14. Verificación (adversarial fact-check, 2026-10-15)

Method: every claim below was re-checked against a source **different** from the one the report cited.
Limitation: the shared web-search budget was exhausted before this pass started, so developers.tiktok.com
could **not** be re-read; checks rely on the three original reference repos cross-checked against each other
plus five extra open-source implementations cloned from GitHub (listed at the end of §13). "CONFIRMED" here
means "independently implemented the same way by at least one other project", not "re-read in the official
docs".

| # | Claim | Verdict | Source used for the check |
|---|---|---|---|
| 1 | Authorize `GET https://www.tiktok.com/v2/auth/authorize/` with `client_key`, comma-separated `scope`; token/refresh `POST https://open.tiktokapis.com/v2/oauth/token/`, form-encoded, `client_key` not `client_id` | CONFIRMED | django-allauth `tiktok/views.py` + `client.py`; SocialiteProviders `TikTok/Provider.php`; Auth.js `providers/tiktok.ts`; Better Auth `social-providers/tiktok.ts` |
| 2 | Access token `expires_in` 86400 (24 h); refresh returns a new `refresh_token` to store | CONFIRMED (24 h) / refresh 365 d UNVERIFIED (single source) | TryPost tests `TikTokControllerTest.php:112`, `TikTokPublisherTest.php:597`; Postiz stores 23 h; all refs persist the returned `refresh_token` |
| 3 | PKCE challenge = **hex** SHA-256, mandatory on Desktop | UNVERIFIED (single code source: Brightbean); made switchable | No other of 8 implementations sends a TikTok PKCE challenge |
| 4 | Desktop platform allows `http://127.0.0.1:<port>` / `localhost` redirects and works with Content Posting | UNVERIFIED; evidence weaker than stated → **CORRECTED** (§1.2): OpenPost's localhost row is generic and OpenPost sends no PKCE | OpenPost `callback-urls.md`, `tiktok.go:79-87`; Better Auth/allauth/Auth.js docs say localhost is rejected (Web platform) |
| 5 | Redirect path must not contain "tiktok" | **CORRECTED** → contradicted in practice; still followed | Postiz `tiktok.provider.ts:353`, OpenPost, TryPost `.env.example:136`, Auth.js docs |
| 6 | `disable_auto_auth=1` is a valid authorize parameter | CONFIRMED | TryPost `TikTokController.php` connect() |
| 7 | Unaudited client: Direct Post only `SELF_ONLY`; max 5 posting users / 24 h (`reached_active_user_cap`); `unaudited_client_can_only_post_to_private_accounts` | CONFIRMED | Postiz `tiktok.provider.ts:218-236`; TryPost `TikTokPublishException.php:72,77`; Brightbean `tiktok.py:49-65` (report had cited OpenPost doc) |
| 8 | `creator_info/query` (POST, empty JSON) returns `creator_nickname`, `creator_username`, `creator_avatar_url`, `privacy_level_options`, `comment/duet/stitch_disabled`, `max_video_post_duration_sec` | CONFIRMED (+ the two "recalled" fields) + **added** gotcha: refusals arrive with HTTP 200 | TryPost `TikTokCreatorInfo.php`, `TikTokSettings.vue` |
| 9 | Privacy dropdown with no default; interactions unchecked by default and greyed out when disabled | CONFIRMED | TryPost `TikTokSettings.vue` (placeholder, defaults `false`, watch on `*_disabled`) and `TikTokPublisher.php:139-156` ("Guideline Point 2b forbids any default"); OpenPost `tiktok.go:600-615` |
| 10 | Branded content cannot be `SELF_ONLY`; labels "Promotional content" / "Paid partnership"; consent line with Music Usage Confirmation + Branded Content Policy URLs | CONFIRMED (was UNVERIFIED) + nuance added (both toggles → "Paid partnership") | TryPost `TikTokSettings.vue`, `lang/en/posts.php:313-345`, `Platform.php:589-590` |
| 11 | Video init body: `post_info.{title, privacy_level, disable_comment, disable_duet, disable_stitch, video_cover_timestamp_ms, brand_content_toggle, brand_organic_toggle, is_aigc}` + `source_info` | CONFIRMED | TryPost `TikTokPublisher.php:165-194`; OpenPost `tiktok.go:305-321` |
| 12 | Caption ≤ 2200 (UTF-16) for video; photo `title` ≤ 90, `description` ≤ 4000 | CONFIRMED (refs agree; Postiz uses 2000) | TryPost `Platform.php:234`, `TikTokPublisher.php:196-203`; OpenPost `tiktok.go:28, 480-484` |
| 13 | FILE_UPLOAD chunking: 5–64 MB chunks, floor count, last chunk ≤ 128 MB, ≤ 1000 chunks; PUT with `Content-Range`, 206 then 201 | CONFIRMED | OpenPost `tiktok.go:419-428` + `tiktok_test.go:498-577`; Postiz `tiktok.provider.ts:715-800` |
| 14 | "All reference projects prefer FILE_UPLOAD for videos" (summary wording) | **CORRECTED**: OpenPost and TryPost Direct Post use PULL_FROM_URL; FILE_UPLOAD Direct Post proven by Postiz + Brightbean only | OpenPost `tiktok.go:296-321`; TryPost `TikTokPublisher.php:225-251` |
| 15 | Status values `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`, `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE`, `FAILED`; field `publicaly_available_post_id` (typo) | CONFIRMED | TryPost `Enums/TikTok/PublishStatus.php`, `TikTokPublisher.php:533-538` |
| 16 | Public post id "never for SELF_ONLY/MUTUAL_FOLLOW_FRIENDS" | **CORRECTED** → only `PUBLIC_TO_EVERYONE` posts get one (also not `FOLLOWER_OF_CREATOR`); consequence for analytics added | TryPost `.ai/rules/tiktok.md`; OpenPost `competitor-audit-2026-10-10.md:45` |
| 17 | Inbox upload: `/v2/post/publish/inbox/video/init/`, only `source_info`, caption not transferable; 5 pending shares / 24 h (`spam_risk_too_many_pending_share`) | CONFIRMED (endpoint, limit); caption point remains contradictory in Postiz itself | OpenPost `tiktok.go:366-465`; TryPost `TikTokPublishException.php:74`; Postiz `tiktok.dto.ts:72-78` vs UI `:62-65` |
| 18 | Photo posts accept only `PULL_FROM_URL` (verified domain), ≤ 35 JPEG/WebP | CONFIRMED; image-size rule **CORRECTED** (refs disagree: shorter side vs width ≤ 1080) | TryPost `TikTokPublisher.php:253-340`; OpenPost `tiktok.go:467-498, 838-845` |
| 19 | Error codes / `fail_reason` list | CONFIRMED + **extended** (`invalid_params`, `invalid_publish_id`, `token_not_authorized_for_specified_publish_id`, spam_risk fail reasons; bare 401 ≠ dead token) | TryPost `TikTokPublishException.php`, `TikTokPublicationCollector.php:42` |
| 20 | Display API: `user/info` stats fields; `POST /v2/video/list/?fields=` with `max_count` 20 + `cursor`/`has_more`; `/v2/video/query/` with `filters.video_ids`; `cover_image_url` expires | CONFIRMED | TryPost `TikTokPublicationCollector.php:17-104`, `TikTokFollowerCollector.php:20-21`, `TikTokPublicationMetricsCollector.php:22` |
| 21 | Display API no longer offered to new apps | still UNVERIFIED officially, now corroborated by a 2nd implementation | TryPost `TikTokController.php`, `.env.example:137` |
| 22 | Video file-size cap 4 GB | corroborated (not official) | OpenPost `docs/reference/providers/index.md:85` |
| 23 | Rate limits 6/min (init), 20/min (creator_info), 30/min (status); Sandbox 10 target users; ~15 posts/day; inbox draft lifetime; revoke needs `client_secret` | UNVERIFIED (no second source reachable in this pass) | — |

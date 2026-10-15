# 02 — Meta APIs (Facebook Pages + Instagram professional accounts) for a LOCAL app

Research date: **2026-10-15**. Target: "Planificador de Redes", a single-user web app that runs on the
user's Windows PC at `http://localhost`, uses the user's **own** Meta developer app, and must publish,
schedule, read analytics and manage comments for Facebook Pages and Instagram professional accounts.

How to read this document

- **Ground truth** = three AGPL reference implementations, cloned and current to Oct 14–15 2026 (read only,
  nothing copied): `postiz-app` (TypeScript), `brightbean-studio` (Python), `openpost` (Go). Paths below are
  relative to each repo root. Two more sources: the MIT-licensed `deegitech/ig-publish` (Oct 2026), which
  publishes Instagram Reels and Stories from local files with **no public URL**, and the Postiz docs repo
  `gitroomhq/postiz-docs`.
- Official Meta facts were checked through web-search snippets of developers.facebook.com. The site itself
  could not be fetched from this container.
- **UNVERIFIED** marks a claim that sources contradict or that no primary source confirmed. Test these early.
- An adversarial verification pass (2026-10-15) re-checked the most load-bearing claims against independent
  sources and corrected several in place; see **§11 Verificación** at the end.

---

## 0. Summary and decisions

| Topic | Decision for our app |
|---|---|
| Graph API version | Default **`v25.0`**, the version all three references pin. Make it a setting and test **v26.0** (current since 2026-07-29). Avoid `GET /?ids=`, `date_format`, `pretty` and `debug`: v26 breaks them on all versions from 2026-10-27. |
| Meta app | The user creates **one Business-type Meta app** with Facebook Login for Business and Instagram (API setup with Facebook login). Keep **Standard Access**: no App Review, because only the app admin uses it. **Switch the app to Live after the first connection**, or Facebook Page posts are visible only to app roles. |
| IG connection | Primary: **Instagram API with Facebook Login** (IG account linked to a Page). It shares the Meta app and OAuth with Facebook, accepts `http://localhost` redirects in dev mode, gives Page tokens that do not expire, and is the only path where resumable local video upload is **proven in production** (ig-publish). Secondary: **Instagram API with Instagram Login** for IG accounts without a Page. It needs an HTTPS redirect; whether it accepts `upload_type=resumable` is disputed (Meta's own SDK lists the parameter, ig-publish says it needs a public URL — see §2.1), so assume a public URL until tested. |
| Facebook media | Always upload the local bytes: `/photos` multipart `source`, the rupload binary for Reels and video Stories, and `/videos` with `source` or the Resumable Upload API. **No public URL or tunnel is ever needed for Facebook.** |
| IG video | Upload local bytes with `upload_type=resumable` and `rupload.facebook.com/ig-api-upload/...` (proven on the Facebook Login path; disputed for Instagram Login, §2.1). |
| IG images | Need a **public HTTPS URL** in 2026; there is no direct image upload. Default: start an on-demand **Cloudflare quick tunnel** that serves only the one JPEG through a single-use URL, on a separate port. Option: the user configures a public base URL (own domain, named tunnel or S3/R2). |
| Scheduling | Our own in-process scheduler. Optional fallback for when the PC may be off: Facebook native scheduling (`published=false` + `scheduled_publish_time`). Instagram has no native API scheduling: Meta's v26 SDK lists no publish-time parameter on `POST /{ig-user-id}/media` (only a read-only `GET /{ig-user-id}/scheduled_media` edge), so the PC must be on. |
| Analytics | Take a **daily snapshot** into SQLite. Meta keeps short lookbacks: IG `follower_count` and `online_followers` cover only 30 days, FB Page insights allow 90 days per call. Collect IG **Story insights before the story expires (24 h)**. Request metrics one at a time on an "invalid metric" error. |
| Comments | **Poll** (webhooks need a permanent public HTTPS endpoint). FB: `/{post}/comments` and reply on `/{comment}/comments`. IG: `/{media}/comments` and reply on `/{comment}/replies`. |
| Rate limits | Treat codes 4, 17, 32, 613, 368, 80000–80009 and 80014 (and HTTP 429) as throttling, and parse `X-App-Usage` / `X-Business-Use-Case-Usage` on every response (§7.1). |
| Reliability | Never blindly retry a publish. Persist the stage (container id, video id, phase) and on failure **read the state first**: a container with status `PUBLISHED` or a video in `publishing_phase complete` is treated as done. Postiz, OpenPost and ig-publish all converge on this. |

---

## 1. Graph API version

- **Current versions (official changelog, via search):** v26.0 was released **2026-07-29** with expiry TBD.
  v25.0 was released **2026-02-18** and is listed as expiring around 2028. v24.0 was released 2025-10-08.
  Sources: <https://developers.facebook.com/docs/graph-api/changelog/versions/>,
  <https://developers.facebook.com/docs/graph-api/changelog/version26.0/>,
  <https://developers.facebook.com/blog/post/2026/02/18/introducing-graph-api-v25-and-marketing-api-v25>.
  Cross-check: Meta's official `facebook-business` SDK on PyPI published 25.0.0 on 2026-03-10 and **26.0.0 on
  2026-08-06** (26.0.2 on 2026-09-21, `apiconfig.py` → `API_VERSION: 'v26.0'`), so v26.0 exists and is current.
  The exact release day (07-29) and the 2026-10-27 all-version enforcement below come only from search snippets
  (UNVERIFIED). Mixpost (MIT, PHP) also defaults to `v25.0` (`src/Services/FacebookService.php:18`).
- **v26.0 changes that matter to us:**
  - The legacy protocol features are deprecated: `pretty` and `debug` are ignored, `date_format` returns an
    error, and root `GET /?ids=` returns an error. These hit v26 on 2026-07-29 and **all versions on 2026-10-27**.
  - Some legacy Page fields (current_location, genre, …) are deprecated for New Pages Experience Pages.
  - No Pages or Instagram publishing change was found (search coverage of the changelog was partial, so UNVERIFIED).
- **How the references pin the version:**
  - Postiz has one constant, `META_GRAPH_API_VERSION = 'v25.0'`
    (`libraries/nestjs-libraries/src/integrations/social/facebook.provider.ts:26`). The Instagram providers
    import it (`instagram.provider.ts:21`, `instagram.standalone.provider.ts:15`).
  - Brightbean hard-codes `https://graph.facebook.com/v25.0` and `https://www.facebook.com/v25.0/dialog/oauth`
    (`providers/facebook.py:40-42`, `providers/instagram.py:53-55`), plus `https://graph.instagram.com/v25.0`
    (`providers/instagram_login.py:56-57`).
  - OpenPost defaults to `"v25.0"` with an env override `META_GRAPH_API_VERSION`
    (`apps/server/internal/platform/facebook.go:18,55-60`).
- **Decision:** add a setting `meta_graph_version` with default `v25.0`. Use the same value in the dialog URL
  (`www.facebook.com/{v}/dialog/oauth`), on the Graph hosts (`graph.facebook.com/{v}`, `graph.instagram.com/{v}`)
  and in the IG rupload path (`/ig-api-upload/{v}/…`). Two exceptions carry no version: the IG Login token
  endpoints (`api.instagram.com/oauth/access_token`, `graph.instagram.com/access_token`,
  `graph.instagram.com/refresh_access_token`) and the `upload_url` that Facebook returns. Add an
  integration-test checklist to run against v26.0.

---

## 2. Authentication and app setup

### 2.1 Which Meta products to use

| | Instagram API **with Facebook Login** | Instagram API **with Instagram Login** ("standalone", Business Login for Instagram) |
|---|---|---|
| Account requirement | IG Business or Creator account **linked to a Facebook Page** that the user manages | IG Business or Creator account; no Page needed |
| Host | `graph.facebook.com/{v}` | `graph.instagram.com/{v}` |
| Login dialog | `https://www.facebook.com/{v}/dialog/oauth` | `https://www.instagram.com/oauth/authorize` |
| App id / secret | Meta App ID and App Secret (same as for FB Pages) | **Instagram App ID and Secret**, shown under "API setup with Instagram login". They differ from the Meta App ID. |
| Token | Long-lived user token (~60 days) → **Page token, no expiry** | IG user token: short-lived (1 h) → long-lived (60 days), refreshable |
| Resumable local upload (`upload_type=resumable`) | **Yes.** ig-publish uses it in production (Oct 2026). A search snippet of Meta's docs says it is limited to apps that implemented Facebook Login for Business (exact wording UNVERIFIED). | **Disputed (UNVERIFIED).** Meta's own generated SDK (`facebook-business` 26.0.2, `adobjects/iguserforigonlyapi.py:279-302`) lists `upload_type` among the `POST /media` parameters of the Instagram-Login API, but ig-publish (`docs/setup.md:122-125`) says Instagram Login "needs a public `video_url` for every file". Plan for a public URL; test resumable on `graph.instagram.com` early. |
| Audio API, `total_*` paid metrics | Yes (Postiz `instagram.provider.ts:787-808`; Meta insights doc) | `audio_configuration` / `audio_name` are listed on the IG-Login `POST /media` in Meta's SDK (`iguserforigonlyapi.py:285-286`), so audio may work (UNVERIFIED; the `ig_audio` search edge is not in that class). `total_*` metrics: Facebook Login only. |
| Stories | Business accounts only. Creator accounts get `(#10) The user is not an Instagram Business`; ig-publish confirms this (`docs/instagram-api.md:27`) and Postiz maps that exact error text (`instagram.provider.ts:143-148`). | Docs do not state the restriction (UNVERIFIED) |
| Redirect URI | `http://localhost` works in dev mode (see 2.4) | Probably **HTTPS only**. Postiz wraps non-HTTPS URLs in `https://redirectmeto.com/…` for Instagram Login and Threads, but **not** for its Facebook or Instagram-with-Facebook-Login providers (`instagram.standalone.provider.ts:116-118,140-143`; `grep redirectmeto` finds no hit in `facebook.provider.ts`/`instagram.provider.ts`). UNVERIFIED whether `https://localhost` is accepted. |

**Facebook Login vs Facebook Login for Business**

- Business-type apps get the **Facebook Login for Business** product. It works with a **configuration**
  (`config_id`) that bundles the token type ("User access token" vs "System-user access token"), the
  permissions and the asset selection.
- Brightbean handles both modes (`providers/meta_oauth.py:22-51`):
  - With a `config_id`, it sends `config_id` and **`override_default_response_type=true`** (to keep the `code`
    flow) and **no `scope`**.
  - Without one, it sends `scope=…`.
  - In both cases it adds `auth_type=rerequest` so permissions the user declined earlier are asked again
    (`meta_oauth.py:19`; OpenPost `facebook.go:73`).
- Postiz and OpenPost send only `scope=` (`facebook.provider.ts:301-315`, `facebook.go:66-75`). The Postiz docs
  still tell users to "Set up login for business" (postiz-docs `self-host/providers/facebook.mdx`). In practice
  the plain `scope` parameter works on business apps.
- **Decision:** support an optional `config_id` field in settings. Use `scope` by default and `config_id` when
  it is filled. The configuration must use the token type **"User access token"**.

### 2.2 OAuth flows (exact calls)

**A. Facebook Login: Pages, and Instagram via Facebook Login**

1. Authorize (browser):
   `GET https://www.facebook.com/v25.0/dialog/oauth?client_id={APP_ID}&redirect_uri={URI}&state={csrf}&response_type=code&scope={csv}&auth_type=rerequest`
   (or `&config_id={CFG}&override_default_response_type=true` instead of `scope`).
2. Exchange the code for a short-lived user token (about 1–2 h):
   `GET|POST https://graph.facebook.com/v25.0/oauth/access_token?client_id&client_secret&redirect_uri&code`.
   The `redirect_uri` must be byte-identical to step 1; Strict Mode rejects extra query parameters.
   Refs: Postiz `facebook.provider.ts:340-352`, Brightbean `facebook.py:187-210`, OpenPost `facebook.go:89-112`
   (OpenPost POSTs form-encoded).
3. Exchange for a long-lived user token (about 60 days):
   `GET https://graph.facebook.com/v25.0/oauth/access_token?grant_type=fb_exchange_token&client_id&client_secret&fb_exchange_token={short}`
   (Postiz `:354-362`, Brightbean `:212-240`, OpenPost `:114-127`).
4. Verify grants: `GET /v25.0/me/permissions` and keep rows with `status=granted` (Postiz `:364-373`, OpenPost
   `:167-191`). A Page token has no `/me/permissions`; inspect it with
   `GET /debug_token?input_token={tok}&access_token={APP_ID}|{APP_SECRET}`, which returns `scopes`,
   `granular_scopes`, `expires_at` and `data_access_expires_at` (Brightbean `facebook.py:1190-1246`).
5. List Pages and their tokens:
   `GET /v25.0/me/accounts?fields=id,name,username,access_token,category,picture,followers_count,tasks,instagram_business_account{id,username,name,profile_picture_url,followers_count,media_count}&limit=100`
   and follow `paging.next` (Brightbean `meta_accounts.py:46-116`, `instagram.py:272-319`).
   - Postiz also walks `GET /me/businesses` → `/{biz}/owned_pages` and `/{biz}/client_pages` (requires
     `business_management`) to find Pages the user did not tick in the dialog (`facebook.provider.ts:392-452`).
   - **Reject Pages whose `tasks` lack `CREATE_CONTENT`**: they connect fine and then every publish fails
     (Brightbean `meta_accounts.py:119-132`).
6. Store per Page: `page_id`, `page_access_token`, `tasks`, and `instagram_business_account.id` (= `ig_user_id`).
   - Use the **Page token** for all Page and IG calls (Brightbean, OpenPost).
   - Keep the long-lived user token too. It is needed to re-list Pages and for the app-scoped
     `/{app-id}/uploads` Resumable Upload API (§3.4).

**B. Instagram API with Instagram Login (standalone)**

1. Authorize:
   `GET https://www.instagram.com/oauth/authorize?client_id={IG_APP_ID}&redirect_uri={URI}&response_type=code&scope=instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments,instagram_business_manage_insights&state={csrf}&enable_fb_login=0&force_authentication=1`
   (Brightbean `instagram_login.py:172-182`; Postiz `instagram.standalone.provider.ts:108-126`).
   Meta appends `#_` to the redirect and it is not part of the code; strip it (UNVERIFIED, from memory of Meta docs).
2. Short-lived token: `POST https://api.instagram.com/oauth/access_token` as **multipart/form-data** with
   `client_id`, `client_secret`, `grant_type=authorization_code`, `redirect_uri` and `code`.
   Response: `access_token`, `user_id`, `permissions` (Brightbean `instagram_login.py:184-216`; Postiz `:133-164`).
3. Long-lived token (60 days):
   `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret={IG_SECRET}&access_token={short}`
   (Brightbean `:218-251`).
4. Refresh: `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token={long}`.
   - The token must be **at least 24 h old and not expired**. The new token is valid 60 days from the refresh.
   - An expired token cannot be refreshed; the user must log in again.
   - Requires `instagram_business_basic`. Refresh around day 45–50, from a daily job.
   - Refs: Meta `instagram-platform/reference/refresh_access_token`; Brightbean `:253-281`; Postiz `:79-106`
     (`refreshCron = true`).
5. Profile: `GET https://graph.instagram.com/v25.0/me?fields=user_id,username,name,profile_picture_url,followers_count,media_count`.
   All later calls use `/me/...` or `/{user_id}/...` on `graph.instagram.com`.

### 2.3 Token lifetimes

| Token | Lifetime | Notes |
|---|---|---|
| FB short-lived user | about 1–2 h | Only used for the exchange |
| FB long-lived user | about 60 days | Can be invalidated early: password change, removed app, checkpoint (190/460, 190/459) |
| **Page token** derived from a long-lived user token | `expires_at: 0` (no expiry) | Stops working if the user token is revoked, the password changes or the Page role is removed (190/492). `data_access_expires_at` (90-day data-access window) is still reported; how it is enforced for Page tokens is **UNVERIFIED**, so check `debug_token` daily and warn when it is under 14 days. Brightbean stores Page tokens with `expires_in=None` (`apps/social_accounts/views.py:369-380`). |
| IG Login long-lived | 60 days | Refreshable when ≥ 24 h old |
| System-user token (Business Manager) | can be non-expiring | Advanced alternative for power users (ig-publish `docs/instagram-api.md:33-35`) |

### 2.4 Redirect URI rules and what works on a user's PC

- **Strict Mode** is mandatory: the URI must match an entry of "Valid OAuth Redirect URIs" exactly, with no
  extra query parameters. Pass dynamic data in `state`. Source: Meta "Login Security",
  <https://developers.facebook.com/docs/facebook-login/security>.
- **Enforce HTTPS**: all Facebook Login redirects must be HTTPS, **except `http://localhost`, which is allowed
  only while the app is in Development mode** (Meta "Requiring HTTPS for Facebook Login", 2018, still quoted by
  the current security page). The Facebook Login settings help text (recalled, not re-fetched: UNVERIFIED wording)
  says localhost redirects are "automatically allowed while in development mode only and do not need to be added".
  Register the URI anyway so that a later Live-mode failure is easy to diagnose.
  - Postiz documents `http://localhost:4200/integrations/social/facebook` and `…/instagram` as valid local
    redirect URIs (postiz-docs `self-host/providers/facebook.mdx:63-64`, `instagram.mdx:85-86`).
- **The Live-mode conflict.** Facebook Page posts from a Development-mode app are **visible only to app roles**:
  "If you do not do this then posts made via the API will display for yourself but will not be visible for
  other users" (postiz-docs `facebook.mdx:83-84,105-109`). So the app must go Live, and in Live mode an HTTP
  localhost redirect may be refused (UNVERIFIED whether this is enforced for app admins in 2026).
- **Recommended procedure (document it in the setup wizard, in Spanish):**
  1. Register `http://localhost:{PORT}/conexiones/meta/callback` (the fixed port is a setting).
  2. Connect Facebook and Instagram while the app is in **Development** mode. Page tokens do not expire.
  3. Switch the app to **Live**. The Basic settings need at least a Privacy Policy URL; the exact list of
     required fields is UNVERIFIED. OpenPost's research (`openpost/docs/research/provider-production-review-requirements-2026-09-19.md:70`,
     citing Meta's "App modes" and "Basic settings" pages) lists for Live: app icon, category, contact email,
     privacy-policy URL, app domains and a user-data-deletion URL or instructions, and states "Live mode authorizes
     anyone, but only for approved permissions and features". The same file (`:68`) says a role-only private app
     is exempt from Tech Provider / Business verification.
  4. If a reconnect is needed later and Meta refuses the HTTP redirect, either toggle back to Development for
     the reconnect, or turn on the app's optional **HTTPS callback listener**
     (`https://localhost:{PORT_TLS}/…`, self-signed certificate generated on first run, one browser warning)
     and register that URI as well.
- **Instagram Login** redirects (registered under "Business login settings → OAuth redirect URIs") are probably
  HTTPS only. Use the same HTTPS-callback listener. Do **not** use `redirectmeto.com` by default: it routes the
  OAuth code through a third party.
- **No public callback is needed for OAuth itself.** The browser follows the redirect, so `localhost` works.
  Public reachability is only needed for webhooks and for Instagram fetching `image_url`.

### 2.5 Permissions (scopes)

**Facebook Pages (Facebook Login)**

| Scope | Why | Refs |
|---|---|---|
| `pages_show_list` | `/me/accounts` | all three |
| `pages_manage_posts` | create, edit and delete Page posts, photos, videos, Reels and Stories | all three |
| `pages_read_engagement` | read posts and engagement, Page metadata | all three |
| `pages_manage_engagement` | **post, hide and delete comments** (first comment, replies) | Postiz `facebook.provider.ts:39`, Brightbean `facebook.py:145-147` |
| `pages_read_user_content` | read comments **including `from`** (user-generated content) | Brightbean `facebook.py:150`, OpenPost docs |
| `read_insights` | Page and post insights | Postiz `:41`, Brightbean `:158-162` |
| `business_management` | Pages owned through a Business portfolio (`/me/businesses`) | all three |
| `pages_manage_metadata` | only for webhooks (`/{page}/subscribed_apps`); **not needed for polling** | Brightbean, OpenPost |

**Instagram with Facebook Login:** `instagram_basic`, `instagram_content_publish`, `instagram_manage_comments`,
`instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`, `business_management`
(Postiz `instagram.provider.ts:36-44`). OpenPost adds `pages_read_user_content` and notes that `instagram_basic`
has a Page dependency on it (openpost `apps/docs/.../instagram.mdx`). ig-publish notes that when the Page role
comes through a Business portfolio, Meta also requires `ads_management` or `ads_read` (`docs/instagram-api.md:30-32`,
UNVERIFIED). Deleting IG media needs `instagram_manage_contents` (ig-publish).

**Instagram Login:** `instagram_business_basic`, `instagram_business_content_publish`,
`instagram_business_manage_comments`, `instagram_business_manage_insights`. Add
`instagram_business_manage_messages` only for DMs, which are out of scope (Postiz `:32-37`, Brightbean `:142-158`).

**Decision:** request a single combined scope set for FB and IG together. The user can create a single
Meta app and connect both in one dialog, which Postiz recommends: "Instagram and Facebook can use the same app".

### 2.6 Development mode, Live mode, App Review

- **Development mode:** all permissions work, but only for people with an **app role** (admin, developer,
  tester). This is enough for a personal tool: the user is the app admin. With Instagram Login, the IG account
  must be added as **Instagram Tester** and accept the invite under instagram.com → Settings → Apps and Websites
  (postiz-docs `instagram.mdx`, "Adding App Roles").
- **App Review / Advanced Access:** not required for a personal tool. "If your Postiz install is for personal use
  only these advanced permissions are not required" (postiz-docs `facebook.mdx:79`). Business apps get
  **Standard Access** automatically.
- **But Facebook Page posts are only visible to app roles while the app is in Development**, so switch to
  **Live** (postiz-docs `facebook.mdx:83-84`). Instagram posts from dev-mode apps appear to be public: ig-publish
  publishes this way in production. This is not stated by Meta (UNVERIFIED).
- Meta's own App Review guide says "only switch to Live after App Review", warns that in Live mode only
  *approved* permissions work, "even for users who have a role on the app", and that dev-mode test posts become
  visible once Live. In practice Standard Access counts as approved for role users, which is why the Postiz
  personal-use recipe works. **UNVERIFIED for every account. Spike this first.**
- Business verification: needed only for Advanced Access or public apps (Postiz: "for public applications, you
  will need to verify your business"). It should not be needed here (UNVERIFIED).

---

## 3. Facebook Page publishing

Use the **Page access token** for everything. Base: `https://graph.facebook.com/v25.0`.

### 3.1 Text and link posts

- `POST /{page-id}/feed` with `message`, optional `link`, and optional `published=false` +
  `scheduled_publish_time` (§3.7). Refs: Brightbean `facebook.py:321-337`, OpenPost `facebook.go:546-574`.
- Optional `text_format_preset_id` (coloured background). Text only, at most about 130 characters. A stale preset
  makes Facebook reject the whole post, so retry once without it (Postiz `facebook.provider.ts:888-969`,
  `FACEBOOK_PRESET_MAX_CHARS = 130`).
- If link scraping fails (error **1609005**), OpenPost retries without `link` (`link_cards.go:211-216`,
  `facebook.go:569-572`).
- Response: `{"id": "{pageid}_{postid}"}`. Get the permalink with `GET /{id}?fields=permalink_url`.
- Message length: 63,206 characters (Postiz `:45-47`, Brightbean `:129-130`).

### 3.2 Photos (single and multiple), from local files

- **Single photo:** `POST /{page-id}/photos` as **multipart/form-data** with the file in **`source`**, plus
  `message` (or `caption`) and `published=true`.
  - Response: `{"id": photo_id, "post_id": "{page}_{post}"}`.
  - The references send `url=` (a hosted file) instead (Brightbean `facebook.py:339-359`, OpenPost
    `facebook.go:576-588`). Meta docs confirm multipart upload. **Mixpost (MIT, PHP) uploads local files exactly
    this way**: multipart field `source` + `published=false` on `/{page}/photos`, then `/feed` with
    `attached_media` (`src/SocialProviders/Meta/Concerns/ManagesMetaResources.php:44-61,91-103`).
- **Multiple photos (up to 10):**
  1. Upload each photo with `POST /{page-id}/photos` (`source` + `published=false`) → `id`. For a scheduled
     multi-photo post, add `temporary=true` (Meta Page Photos reference, via search; UNVERIFIED wording).
  2. `POST /{page-id}/feed` with `message` and `attached_media=[{"media_fbid":"…"},…]` as JSON, or form fields
     `attached_media[0]={"media_fbid":"…"}`.
  3. On failure, **delete the staged photos** (`DELETE /{photo-id}`).
  - Refs: Brightbean `facebook.py:361-438` (max 10 at `:96`), OpenPost `facebook.go:606-640`, Postiz `:863-923`.
- **Size and format:**
  - Meta reference: ≤10 MB (one mirror says 4 MB).
  - Postiz maps error **1366046** to "Photos should be smaller than 4 MB and saved as JPG, PNG"
    (`facebook.provider.ts:108-113`).
  - **Decision:** re-encode to JPEG and keep files ≤4 MB (Pillow) before uploading. OpenPost accepts
    JPEG/PNG/WebP (`facebook.go:844-887`).
- Error **1366051** means "These photos were already posted" (duplicate guard).

### 3.3 Reels (`/video_reels`, three phases, local bytes)

1. Start: `POST /{page-id}/video_reels` with `upload_phase=start` → `{"video_id", "upload_url"}`.
   - `upload_url` points to `https://rupload.facebook.com/video-upload/{v}/{video_id}`.
   - **Validate the host before sending the token** (OpenPost `facebook.go:461-468`).
2. Upload: `POST {upload_url}`.
   - Local file: headers `Authorization: OAuth {page_token}`, `offset: 0`, `file_size: {bytes}`, body = raw bytes
     (`application/octet-stream`).
   - Hosted file: header `file_url: {public_url}` instead of a body (Brightbean `facebook.py:516-532`, OpenPost
     `facebook.go:405-408`).
   - **Use the `OAuth` scheme, not `Bearer`** (Brightbean comment; developer reports).
   - Success: `{"success": true}`. Treat anything else as a failure, including HTTP 200 bodies that contain an
     `error` object.
3. Wait for the upload: poll `GET /{video_id}?fields=status` until `status.uploading_phase.status == "complete"`
   or `status.video_status in {"upload_complete","ready"}`. Fail on any phase `error` or
   `video_status in {error, expired}`. OpenPost polls 60 × 5 s (`facebook.go:470-534`).
4. Finish: `POST /{page-id}/video_reels` with `upload_phase=finish`, `video_id`, and
   `video_state=PUBLISHED` (or `DRAFT`, or `SCHEDULED` + `scheduled_publish_time`). Also `description`, plus
   optional `title` and `share_to_feed` (OpenPost `facebook.go:999-1014`; Brightbean `facebook.py:534-550`).
5. Confirm: publishing is asynchronous. Poll `status.publishing_phase.status == "complete"` or
   `video_status == "published"` (OpenPost `ResumePublish`, `facebook.go:915-972`). Then
   `GET /{video_id}?fields=post_id,permalink_url`. This is best effort: never fail a live post over metadata
   (Brightbean `:552-582`).

**Reels limits** (Meta Reels Publishing guide, via search):

- 9:16; 1080×1920 recommended, **540×960 minimum**; 24–60 fps; **3–90 s** (Brightbean enforces this at
  `facebook.py:90-92`).
- At most **30 API-published Reels per Page per rolling 24 h**, enforced on `POST /{page}/video_reels`.
- Meta's guide reportedly **rejects `fbcdn` URLs** as `file_url`, and hosted files must allow Meta's
  external-hit user agent.
- MP4 (H.264 + AAC) recommended.
- `scheduled_publish_time` for Reels: about 29 days maximum (third-party summary; UNVERIFIED).

### 3.4 Regular videos (`/videos`)

- **Simple:** `POST https://graph.facebook.com/v25.0/{page-id}/videos` multipart with `source`, plus
  `description` and optional `title`, `published` and `scheduled_publish_time`.
  - Host: Meta's video guides historically use `graph-video.facebook.com`, but **all four code references post to
    `graph.facebook.com`** (Postiz, Brightbean `facebook.py:446`, OpenPost `facebook.go:599`, Mixpost
    `MetaProvider.php:27`). Default to `graph.facebook.com`; keep `graph-video` as a fallback setting.
  - The references use `file_url` with a hosted file (Postiz `facebook.provider.ts:835-861`, Brightbean
    `facebook.py:440-477`, OpenPost `facebook.go:590-604`).
  - **Local bytes, legacy chunked flow:** Mixpost uploads local videos with `upload_phase=start` (`file_size`) →
    repeated `upload_phase=transfer` (`upload_session_id`, `start_offset`, multipart `video_file_chunk`) until
    `start_offset == end_offset` → `upload_phase=finish` (+ `description`) on `/{page}/videos` with the Page token
    (`ManagesMetaResources.php:106-175`). Mixpost's last commit is 2026-03-16, so this flow was in use with
    v25.0 recently; still UNVERIFIED on v26.
  - Older docs list 1 GB / 20 min for non-resumable uploads and 1.5 GB / 45 min for chunked ones. **The 2026
    limits are UNVERIFIED.**
- **Large files, current Meta guide ("Publish a Video", Resumable Upload API):**
  1. `POST /{APP_ID}/uploads?file_name&file_length&file_type&access_token={USER_TOKEN}` → `{"id": "upload:…"}`.
  2. `POST https://graph.facebook.com/v25.0/upload:{session}` with headers `Authorization: OAuth {USER_TOKEN}`
     and `file_offset: 0`, body = bytes → `{"h": "{file_handle}"}`.
     To resume: `GET upload:{session}` → `file_offset`.
  3. `POST https://graph-video.facebook.com/v25.0/{page-id}/videos` with `fbuploader_video_file_chunk={h}`,
     `title`, `description` and the **Page token**.
  - This step needs a **user token** for an app-role user, so the 60-day user token must stay valid.
    The legacy `upload_phase=start|transfer|finish` chunked flow on `/{page}/videos` (Page token only) is used by
    Mixpost (see above), which avoids the user-token dependency.
- **Decision:**
  - Vertical videos of 3–90 s publish as Reels (§3.3, no user token needed).
  - Other videos use multipart `source` up to an UNVERIFIED size threshold, else the legacy chunked flow with the
    Page token, with the Resumable Upload API as the fallback.
  - Poll `GET /{video-id}?fields=status` as in §3.3.

### 3.5 Stories

- **Photo story:**
  1. `POST /{page-id}/photos` with `source` + `published=false` → `photo_id`.
  2. `POST /{page-id}/photo_stories` with `photo_id` → `{"success": true, "post_id"}`.
  - Refs: OpenPost `facebook.go:642-665`, Postiz `facebook.provider.ts:603-620,720-732`.
- **Video story:**
  1. `POST /{page-id}/video_stories` with `upload_phase=start` → `video_id`, `upload_url`.
  2. Upload as in §3.3: binary with `offset` and `file_size`, or a `file_url` header.
  3. Poll `status`.
  4. `POST /{page-id}/video_stories` with `upload_phase=finish&video_id=…` → `{"success": true, "post_id"}`.
  - Refs: Postiz `:578-601,720-732`, OpenPost `:999-1002`.
- **Specs:**
  - Video: 3–60 s (a Reel shared as a Story is capped at 60 s), 9:16, 1080×1920 recommended, ≥540×960.
  - Photo: ≤10 MB (Meta) / 4 MB (third-party).
  - One media item per story. Postiz publishes each media item as a separate story, **one at a time**, with an
    arm/confirm handshake: Facebook has no API to ask whether a story was published, so a crash mid-publish must
    never republish (`facebook.provider.ts:643-754`).
- Native scheduling of Stories through the API: Meta's v26 SDK lists `video_state` (`DRAFT|PUBLISHED|SCHEDULED`)
  and `scheduled_publish_time` on `POST /{page}/video_stories` (`facebook_business/adobjects/page.py`,
  `create_video_story`), while `/photo_stories` takes only `photo_id`. Video-story scheduling may therefore work
  but is untested anywhere (UNVERIFIED). Use our own scheduler for Stories.

### 3.6 `published=false`

- Uploads a photo or video without a feed story. This is used for multi-photo staging, photo stories and
  scheduling.
- Error "Unpublished posts must be posted to a page as the page itself" means a user token was used instead of
  the Page token (Postiz `:86-98`).

### 3.7 Native scheduling (`scheduled_publish_time`)

- **Feed, link and multi-photo posts:** `POST /{page-id}/feed` with `published=false` and
  `scheduled_publish_time`, given as a UNIX timestamp (or ISO 8601).
  - Window: **10 minutes to 30 days** after the request (Meta Pages API "Posts" guide).
  - The Page Feed reference reportedly allows up to 75 days and Page Videos up to 6 months (third-party
    summary; UNVERIFIED). **Use 10 min – 29 days to be safe.**
- **Reels:** `video_state=SCHEDULED` + `scheduled_publish_time` in the finish phase.
- **List and cancel:** `GET /{page-id}/scheduled_posts`; `DELETE /{post-id}`. The edge exists in Meta's v26 SDK
  (`page.py`, `get_scheduled_posts` → `PagePost`). The SDK also confirms `published`, `scheduled_publish_time`,
  `attached_media` and `text_format_preset_id` on `POST /{page}/feed`, `temporary` and `scheduled_publish_time` on
  `POST /{page}/photos`, and `video_state=SCHEDULED` + `scheduled_publish_time` on `POST /{page}/video_reels`.
  None of the four code references uses native scheduling, so the windows below remain UNVERIFIED.
- **Decision:** our scheduler is the default. Add a per-post option "Programar de forma nativa en Facebook" that
  hands the post to Meta when it is due more than 10 minutes ahead, so it publishes even if the PC is off. Media
  is then uploaded immediately.

---

## 4. Instagram publishing (container flow)

Base: `https://graph.facebook.com/v25.0/{ig-user-id}` (Facebook Login, Page token) or
`https://graph.instagram.com/v25.0/me` (Instagram Login).

### 4.1 The flow

1. **Create the container:** `POST /{ig-user-id}/media` with the parameters below → `{"id": container_id}`.
2. **Wait** until `GET /{container_id}?fields=status_code,status` returns `FINISHED`.
   - `status_code` values: `IN_PROGRESS`, `FINISHED`, `PUBLISHED`, `ERROR` (then `status` carries a subcode), and
     `EXPIRED` (not published within **24 h**). Source: Meta IG Container reference.
   - Poll interval in the references:
     - Brightbean: 2 s × 60 (`instagram.py:93-95,426-452`)
     - OpenPost: 10 s × 6 (`instagram.go:675-697`)
     - Postiz: 30 s up to 8 min (`instagram.provider.ts:1052-1092`)
   - A third-party guide quotes Meta as "once per minute, ≤5 minutes" (UNVERIFIED).
   - **Decision:** poll every 5 s for the first minute, then every 15 s, up to 10 minutes for video. Images are
     usually ready in seconds.
3. **Publish:** `POST /{ig-user-id}/media_publish` with `creation_id={container_id}` → `{"id": media_id}`.
4. **Permalink:** `GET /{media_id}?fields=permalink,media_product_type,timestamp`. This is best effort; never
   fail a live post over it (Postiz `instagram.provider.ts:702-720`).

**Idempotency** (Postiz `:854-919`, OpenPost `instagram.go:962-1213`, ig-publish `docs/instagram-api.md`
"Behaviour we design for"):

- Containers are invisible until published, so recreating one is safe.
- `media_publish` is **not** safe to repeat. Before any retry, read the container. If `status_code == PUBLISHED`,
  the post is live: record it and stop.
- Meta can answer HTTP 200 with an `error` object; treat that as an error (ig-publish, observed Oct 2026).

### 4.2 Media types and parameters

| Kind | Container parameters |
|---|---|
| Single image (feed) | `image_url` (**JPEG**), `caption`, optional `alt_text` (OpenPost `instagram.go:383-385`), `user_tags=[{username,x,y}]` (≤20, x and y in 0–1; OpenPost `:386-392,408-439`), `location_id`, `collaborators` |
| Reel (any single video; there is no plain feed video) | `media_type=REELS`, `video_url` **or** `upload_type=resumable`, `caption`, `share_to_feed` (bool), `cover_url` (public JPEG) **or** `thumb_offset` (ms), `audio_name`, `collaborators`, `user_tags`, `location_id`, `trial_params={"graduation_strategy":"MANUAL"\|"SS_PERFORMANCE"}` (trial Reels; Postiz `:773-780`, OpenPost `:366-373`), `audio_configuration={audio_id,audio_volume,video_volume}` (Facebook Login only; Postiz `:787-808`, audio search `GET /ig_audio?audio_type=music\|original_sound&user_id={ig}&search_query=`, `:1255-1284`) |
| Story | `media_type=STORIES` + `image_url` or `video_url` / `upload_type=resumable`. **No caption** (ig-publish). No stickers, links or music. Business accounts only (§2.1). One container per story, published sequentially (Postiz `:937-974`). |
| Carousel child | `is_carousel_item=true` + `image_url`, or `media_type=VIDEO` + `video_url`/resumable. No caption, collaborators or location on children (Postiz `:782-785`, OpenPost `:356-365`). |
| Carousel parent | `media_type=CAROUSEL`, `children={id1,id2,…}` (**2–10**), `caption`, `collaborators`, `location_id`. Children must be `FINISHED` first, or you get an `invalid_children` error. |

- **Collaborators** take a JSON array of usernames with no leading `@`. A leading `@` triggers error 2207018
  (Postiz `:656-660`).
- Maximum 3 collaborators (UNVERIFIED). Not allowed on Stories, and only on the carousel parent (error
  "param collaborators is not allowed").
- `share_to_feed` applies to Reels only. ig-publish defaults it to `true`.
- Caption: ≤2,200 characters, ≤30 hashtags, ≤20 @mentions (ig-publish `docs/instagram-api.md:75`).

### 4.3 The public-URL problem and resumable upload

**Images: no direct upload exists in 2026.**

- `image_url` must be a publicly fetchable HTTPS URL. Meta's servers download it at call time. Signed URLs with a
  short lifetime, basic auth, IP allow-lists or interstitial pages all fail, usually with **9004 / 2207052** or
  **2207003** (timeout).
- No reference implements an image upload. ig-publish states images and carousels are "not implemented"; the API
  supports them only via URL, and ig-publish converts image Stories to short videos to avoid hosting
  (`README.md:76,479`; `docs/instagram-api.md:64-66`).
- The IG User Media reference shows resumable examples **only for Reels, carousel video items and video
  Stories**. No image example exists (UNVERIFIED whether images are accepted).

**Video: resumable upload of local bytes (Facebook Login path)**

1. `POST /{ig-user-id}/media` with `media_type=REELS|STORIES|VIDEO` (carousel child: + `is_carousel_item=true`),
   `upload_type=resumable` and the usual Reel fields (`caption`, `share_to_feed`, `thumb_offset`).
   → `{"id": container_id, "uri": "https://rupload.facebook.com/ig-api-upload/{v}/{container_id}"}`.
2. `POST https://rupload.facebook.com/ig-api-upload/{v}/{container_id}` with headers
   **`Authorization: OAuth {token}`**, **`offset: 0`**, **`file_size: {bytes}`**,
   `Content-Type: application/octet-stream` and `Content-Length`. Body = the whole file.
   - Success: exactly `{"success": true}`.
   - With a hosted file you send a `file_url` header instead of a body.
3. Poll `status_code` (also `video_status`) until `FINISHED`, then call `media_publish`.

- Refs: ig-publish `src/ig_publish/graph.py:281-310`, `publisher.py:571-579`, `docs/instagram-api.md:39-49`.
  Meta content-publishing guide: "available only to apps that have implemented Facebook Login for Business".
- **Gotchas:**
  - ig-publish never resumes mid-file. A failed upload gets a **new container**. rupload can time out mid-file
    on slow uplinks ("observed Oct 2026", `hints.py`).
  - Check that the `uri` host is `rupload.facebook.com` and the path starts with `/ig-api-upload/` before
    attaching the token.
  - Postiz and Brightbean do **not** use this: they host files publicly (Postiz storage, Brightbean
    `APP_URL` or presigned R2 URLs, `apps/publisher/engine.py:724-776`). OpenPost requires
    `OPENPOST_MEDIA_URL` (`instagram.go:307-311`).

**Options for images from a localhost app**

| Option | Verdict |
|---|---|
| (a) **Temporary public tunnel**: Cloudflare quick tunnel (`cloudflared tunnel --url http://127.0.0.1:{PUBPORT} --no-autoupdate`) gives a random `https://*.trycloudflare.com` with **no account**. A single Windows binary from GitHub releases. Cloudflare labels it testing-only: no uptime promise, random host, about 200 concurrent requests. | **Recommended default.** Start it on demand when an IG job with images begins. Serve **only** `GET /p/{random-128-bit-token}.jpg` from a **separate minimal ASGI app on a separate port** that never exposes the UI, with tokens that expire in about 30 minutes. Stop the tunnel when all containers are `FINISHED`. Test that the URL is fetchable (HEAD from the app through the tunnel) before creating containers. ngrok needs an account, and its free tier shows an interstitial to browser-like user agents; it is unknown whether Meta's fetcher triggers it, so avoid ngrok free as the default. |
| (b) **Upload to the FB Page as an unpublished photo and reuse its fbcdn URL** | **Not recommended.** Undocumented, and needs a linked Page (impossible for Instagram Login accounts). fbcdn URLs are signed and expire (`oe=`), may be served as WebP, leave orphan unpublished photos, and Meta's Reels guide explicitly rejects fbcdn URLs as `file_url`. No reference does it. UNVERIFIED whether IG accepts them. |
| (c) User-provided public storage: an S3/R2/B2 bucket (presigned URL ≥1 h) or their own domain or named Cloudflare tunnel | Offer as an **advanced setting** ("URL pública base"). This is the most reliable option for heavy users. |
| (d) Turn image **Stories** into 5 s H.264 videos and use resumable upload (ig-publish approach) | A good **fallback for Stories only**, with no exposure at all. Needs ffmpeg. |
| (e) Third-party image hosts (imgbb, Cloudinary…) | No: privacy risk and dependency, and Instagram rejects some hosts (community reports). |

**Decision:**

- Videos (Reels, video Stories, carousel videos) always use the resumable upload.
- Images use (a), with (c) as an override and (d) as an option for Stories.
- A custom Reel cover (`cover_url`) also needs (a)/(c); otherwise use `thumb_offset`.
- Under Instagram Login, every media item needs (a)/(c) until a test shows that `upload_type=resumable` works there
  (Meta's SDK lists the parameter for the Instagram-Login `/media` edge; ig-publish says it does not work; §2.1).

### 4.4 Limits and media specs (Instagram)

- **Publishing quota:** at most **100 API-published posts per rolling 24 h** per account; a carousel counts as 1.
  - Some Meta pages still say 50, and the 2207042 messages in Postiz (`instagram.provider.ts:316`) and OpenPost
    (`errors.go:241`) say 25 (stale text). ig-publish observed `quota_total=100` in October 2026, and Brightbean
    configures `publish_per_day=100` (`instagram.py:173-180`). Never hard-code it.
  - **Read it live:** `GET /{ig-user-id}/content_publishing_limit?fields=config,quota_usage`, which returns
    `quota_usage` and `config.quota_total`/`quota_duration`. Error **2207042** means the limit is reached.
  - Refs: Meta content-publishing guide (via search); ig-publish `docs/instagram-api.md:72`; Brightbean
    `instagram.py:172-179`.
- **Images:** JPEG only (official; extended JPEG such as MPO/JPS is not supported), ≤8 MB, aspect ratio
  **4:5 to 1.91:1**, width 320–1440 px (scaled), sRGB.
  - Errors: 2207009 / 36003 aspect ratio; 36001 resolution; 2207004 too large; 2207005 format.
  - OpenPost accepts PNG/WebP (`instagram.go:1291-1298`); the official docs say JPEG. **Always convert to JPEG.**
- **Story image:** JPEG ≤8 MB, 9:16 recommended.
- **Story video:** **3–60 s**, ≤100 MB (ig-publish `media.py:29-32`, docs table).
- **Reels:**
  - **3 s – 15 min**, ≤300 MB, MOV/MP4 (H.264 or HEVC), AAC ≤128 kbps, 9:16 recommended.
  - ≤1920 px width and ≤25 Mbps (Postiz error 2207053 text, `instagram.provider.ts:189-195`; ig-publish
    `MAX_VIDEO_BITRATE=25_000_000`).
  - Reels between about 90 s and 15 min may not appear in the Reels tab (Agorapulse).
- **Carousel video items:** about 60 s and 100 MB (UNVERIFIED).
- Container lifetime is 24 h; ig-publish recreates containers older than 23 h.
- **Silent-audio problem:** error 2207082 when audio is attached to a video without an audio track. Retry with the
  original video volume set to 0 (Postiz `:394-400`).

### 4.5 First comment

- `POST /{ig-media-id}/comments` with `message={text}`.
- **Do not pass a `fields` parameter.** According to Brightbean, Meta creates the comment and *then* answers with
  error 20 / subcode **1772107**, which leads to duplicates on retry (Brightbean `instagram.py:473-489`).
  **Sources disagree on the outcome:** Postiz, which never sends `fields` on this POST
  (`instagram.provider.ts:1107-1116`), maps 1772107 to "Instagram did **not** add the comment, the post itself was
  published" (`:386-392`). So 1772107 is **not** proof that the comment exists.
- On 1772107 or any ambiguous failure: list the media's comments and look for an own comment with the same text
  (Brightbean `find_own_comment`). If found, record it; if not, retry **once** without `fields`; never loop.
- Facebook equivalent: `POST /{post-id}/comments` with `message`, optional `attachment_url`; needs
  `pages_manage_engagement` (Postiz `facebook.provider.ts:987-1025`).

---

## 5. Insights (state as of Oct 2026)

General rules:

- Insights requests fail as a whole on a single invalid metric (error code 100, "must be a valid insights metric").
  **Request metrics in a batch and fall back to one at a time on a 400**, as OpenPost
  (`analytics_meta.go:279-305`) and Brightbean (`fetch_insights_safe`, `meta_insights.py:70-109`) do.
- Re-raise permission errors (codes 10/190/200) instead of swallowing them (`meta_insights.py:13,47-67`).
- Values can be numbers or breakdown objects; sum objects (Postiz `facebook.provider.ts:1053-1063`, OpenPost
  `analytics_meta.go:355-407`).
- Prefer `total_value` over `values[]` when both are present.

### 5.1 Facebook Page (account level)

- **Followers:** `GET /{page-id}?fields=followers_count,fan_count`. `fan_count` (likes) is a fallback (OpenPost
  `analytics_meta.go:49-72`).
- **Insights:** `GET /{page-id}/insights?metric=…&period=day&since={unix}&until={unix}`.
  - **At most 90 days per request**; only the **last 2 years** are available. (The API error text is recalled as
    "There cannot be more than 93 days … between since and until"; UNVERIFIED. 90 is the safe chunk size.)
  - Most metrics update once every 24 h.
  - **Pages need ≥100 likes** for Page Insights.
  - Source: Meta Page Insights reference.
- **Current metrics used by the references:**
  - `page_media_view` (content views; can carry breakdowns such as `is_from_ads` / `is_from_followers`)
  - `page_total_media_view_unique` (unique viewers ≈ reach)
  - `page_post_engagements`
  - `page_follows` (lifetime total)
  - `page_daily_follows`, `page_daily_follows_unique`
  - `page_daily_unfollows_unique` (UNVERIFIED)
  - Refs: Brightbean `facebook.py:49-55` and `development_specs/meta-analytics-manual-tests.md`;
    Postiz `:1035-1045`.
- **Deprecated, returning an invalid-metric error:**
  - **2025-11-15:** `page_impressions*`, `page_fans`, `page_fans_*` (locale, city, country), `post_impressions*`
    (Meta blog 2025-08-15 "Page Insights API updates"). Use views and follows instead.
  - **2026-06-15:** `page_impressions_unique`, `page_posts_impressions*`, paid/viral variants,
    `page_fan_adds/removes`, `post_impressions_unique` variants, `*_video_views_unique`
    (Meta insights reference notice; Emplifi, Windsor, Improvado).
  - **Conflict on `page_video_views`:** Postiz says it was deprecated on 2026-06-15 (`facebook.provider.ts:1035-1040`);
    Windsor says only the unique variant was. UNVERIFIED. `page_views_total` is UNVERIFIED as well.
  - Dataslayer mentions a further removal wave on 2026-08-06 without listing the fields (UNVERIFIED).

### 5.2 Facebook post, video and story

- **Feed post:** `GET /{page-post-id}/insights?metric=post_media_view,post_total_media_view_unique,post_clicks,post_reactions_by_type_total`
  (Brightbean `:56-61`; Postiz also requests `post_clicks_by_type`; OpenPost requests
  `post_reactions_like_total`). Add engagement fields with
  `GET /{post}?fields=reactions.limit(0).summary(true),comments.limit(0).summary(true),shares` (OpenPost
  `:118-149`).
- **Video / Reel** (a bare numeric id has no `insights` edge): `GET /{video-id}/video_insights?metric=fb_reels_total_plays,post_video_likes_by_reaction_type,post_video_social_actions,total_video_views`.
  - `total_video_impressions` is no longer accepted.
  - Reels return only the reel metrics.
  - Refs: Postiz `:1176-1278`, OpenPost `:111-115`.
  - Post ids look like `{pageid}_{postid}`; video ids are bare. Postiz uses the shape to choose the edge
    (`:1092-1103`).
- **Story:** `GET /{story-id}/insights?metric=page_story_impressions_by_story_id,page_story_impressions_by_story_id_unique,story_interaction,pages_fb_story_replies,pages_fb_story_shares,pages_fb_story_thread_lightweight_reactions`
  (OpenPost `analytics_meta.go:93-101`). Availability is UNVERIFIED; collect within 24 h.

### 5.3 Instagram account level

- **Profile counters:** `GET /{ig-user-id}?fields=followers_count,follows_count,media_count,username,profile_picture_url`.
  The **follower history** has to come from our own daily snapshots.
- **Time series:** `GET /{ig-user-id}/insights?metric=reach&period=day&since&until`.
- **Totals:** `GET /{ig-user-id}/insights?metric=views,accounts_engaged,total_interactions,likes,comments,shares,saves,replies,follows_and_unfollows,profile_links_taps&metric_type=total_value&period=day&since&until`
  (Brightbean `instagram.py:541-579`; Postiz `:1179-1227`).
- **`follower_count`:** `period=day`, **last 30 days only**, accounts with **≥100 followers** only. One vendor
  claims it was removed in v22+; Meta's docs still list it. Evidence that it still works on v25: Postiz (code as of
  2026-10-15) requests `metric=follower_count,reach&period=day` in a single call (`instagram.provider.ts:1191`),
  and a removed metric would fail that whole call. Still test it (UNVERIFIED for v26).
- **`online_followers`:** `period=lifetime`, last 30 days, ≥100 followers.
- **Demographics:** `metric=follower_demographics|engaged_audience_demographics`, `period=lifetime`,
  `timeframe=this_week|this_month`, `breakdown=age|gender|country|city`, `metric_type=total_value`.
  - **No `since`/`until`.** Top 45 entries only.
- Data can lag **up to 48 h**.
- **Max `since`/`until` span:** 30 days per request (UNVERIFIED). Retention: docs conflict between 90 days and
  2 years (UNVERIFIED). Our snapshots make both irrelevant.
- **Deprecated** (v22 changelog, all versions from **2025-04-21**): `impressions`, `plays`, `clips_replays_count`,
  `ig_reels_aggregated_all_plays_count`, `video_views`.
  - Account-level `profile_views`, `website_clicks`, `email_contacts`, `phone_call_clicks`,
    `text_message_clicks` and `get_directions_clicks` are deprecated.
  - Brightbean dropped `profile_views` (`development_specs/meta-analytics-manual-tests.md`).
  - `website_clicks` → `profile_links_taps` (UNVERIFIED mapping).
  - Metricool itself switched to "Views": <https://help.metricool.com/instagram-replaces-impressions-with-views-what-you-need-to-know-f6n8j>.

### 5.4 Instagram media level

- **Fields:** `GET /{ig-user-id}/media?fields=id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count`
  (Brightbean `instagram.py:80-91`).
- **Insights:** `GET /{media-id}/insights?metric=views,reach,likes,comments,shares,saved,total_interactions`
  (Brightbean `:71-79`; Postiz `:1300`; OpenPost `:202-207`).
  - Optional extras: `follows`, `profile_visits`, `profile_activity`.
  - Reels: `ig_reels_avg_watch_time`, `ig_reels_video_view_total_time`.
  - Stories: `navigation` (breakdown `story_navigation_action_type`), `replies` (returns 0 in the EU and Japan).
  - The extras are UNVERIFIED for 2026; request them one by one.
- **New in 2026** (Meta blog 2026-04-22, "Instagram API updates for partnerships metrics…"):
  - `reposts`
  - `facebook_views`, now covering Feed and Stories as well as Reels
  - `total_likes`, `total_comments`, `total_views` include paid engagement and are available with Facebook Login only.
  - Cross-check: the media-insights `Metric` enum in Meta's SDK 26.0.2 (`adobjects/instagraminsightsresult.py:44-67`)
    contains `views`, `reach`, `reposts`, `facebook_views`, `crossposted_views`, `total_likes`, `total_comments`,
    `total_views`, `reels_skip_rate`, `link_clicks`, and no longer contains `plays`, `video_views`,
    `clips_replays_count` or `ig_reels_aggregated_all_plays_count` (it still lists `impressions`, probably stale).
    `IGMedia` fields now include `shares_count`, `saved_count`, `reposts_count` and `total_*_count`.
- Missing data returns an **empty `data` array, not 0**. Store NULL, not 0.
- No insights for carousel children.
- **Story insights are available only while the story is live (24 h).** Schedule a capture job at about +23 h
  after publishing (UNVERIFIED exact window).

### 5.5 Analytics design decisions

- Store the raw daily values per account and metric in SQLite (`metric_daily(account_id, metric, date, value, breakdown_json)`),
  plus per-post snapshots at +1 h, +24 h, +72 h, +7 d and +28 d.
- Backfill on connect: IG reach/views for 30 days, FB for up to 90 days per call, chunked back to about
  2 years if wanted.
- UI labels (Spanish):
  - "Visualizaciones" (views)
  - "Alcance / Espectadores" (reach / `*_media_view_unique`)
  - "Interacciones"
  - "Seguidores"
  - Never show "Impresiones".

---

## 6. Comments (inbox)

### 6.1 Facebook

- **List the Page's recent posts with nested comments:**
  `GET /{page-id}/feed?fields=id,created_time,permalink_url,comments.limit(50){id,message,created_time,from,parent,permalink_url,can_comment,can_hide,is_hidden}&limit=25&since={30 days ago}`
  (Brightbean `facebook.py:921-980`).
  - `since` filters **posts** by creation time, not comments. Filter comments on their own `created_time`.
- **Per post:**
  `GET /{post-id}/comments?fields=id,from,message,created_time,is_hidden,can_hide,can_comment,parent,comments.order(reverse_chronological){…}&order=reverse_chronological`
  (OpenPost `facebook.go:736-755`). Follow `paging.next`, but only to the `graph.facebook.com` host and with the
  token re-added (Brightbean `:982-1001`).
- **Reply:** `POST /{comment-id}/comments` with `message`.
  - Facebook threads only two levels deep: to answer a reply, post on its **parent** comment.
  - A `parent` equal to the post id means a top-level comment; reply on the comment itself
    (Brightbean `:1100-1166`).
- **Hide:** `POST /{comment-id}` with `is_hidden=true`. **Delete:** `DELETE /{comment-id}` (OpenPost `:792-809`).
- **Permissions:** `pages_read_engagement` + `pages_read_user_content` (author `from`) + `pages_manage_engagement`
  (write).
- Skip the Page's own comments, i.e. those where `from.id == page_id`.

### 6.2 Instagram

- **Recent media:** `GET /{ig-user-id}/media?fields=id,timestamp,permalink,comments_count&limit=25`.
- **Comments:** `GET /{media-id}/comments?fields=id,text,timestamp,username,from{id,username},hidden,like_count,replies{id,text,timestamp,username,from,parent_id}`
  (OpenPost `instagram.go:614-646`).
  - `from` and `replies` may be refused for some app/permission combinations. Degrade one axis at a time,
    keeping `username` as the fallback author signal (Brightbean `meta_comments.py:48-74`).
  - The read field is `text`; the write field is `message` (`meta_comments.py:93-101`).
- **Reply:** `POST /{comment-id}/replies` with `message`.
  - Instagram threads one level: a reply-to-a-reply goes on the top-level comment's `/replies`.
  - Caption mentions are answered with `POST /{media-id}/comments`.
  - Refs: OpenPost `:568-578`; Brightbean `resolve_comment_reply_target`, `meta_comments.py:175-204`.
- **Hide:** `POST /{comment-id}` with `hide=true`. **Delete:** `DELETE /{comment-id}` (OpenPost `:656-673`).
- **Permissions:** `instagram_manage_comments` (Facebook Login) or `instagram_business_manage_comments`
  (Instagram Login).

### 6.3 Webhooks vs polling

- Webhooks (FB `feed`, IG `comments`/`mentions`) need a **permanent public HTTPS callback** and app-dashboard
  subscription (Brightbean `facebook.py:1126-1143`, `instagram.py:764-825`). They are not practical for a
  localhost app.
- **Decision:** poll every 10–15 min while the app runs, plus "Actualizar" on demand.
  - Scan the last 30 days of media (25 posts) with a 24 h overlap window.
  - Deduplicate by comment id (Brightbean's poll constants, `facebook.py:77-88`, `meta_comments.py:24-43`).

---

## 7. Rate limits and errors worth handling

### 7.1 Rate limits

- **Platform rate limit** (user and app tokens): 200 × users per rolling hour. Error **4** = app limit,
  **17** = user limit.
- **Business Use Case (BUC) limits** apply to Pages and Instagram and take precedence when both apply:
  - Pages: about 4,800 × engaged users per 24 h (UNVERIFIED formula). Error **32** (Page request limit),
    **80001** (Pages BUC).
  - Instagram: impression-based, 24 h. Error **80002**.
  - Error **613** = calls within one hour exceeded.
  - Classify the whole BUC family as rate limiting, not only 80001/80002: Mixpost treats
    `4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014` as rate-limit errors and
    reads `X-App-Usage` / `X-Business-Use-Case-Usage` on every response, pausing above 90%
    (`src/SocialProviders/Meta/Concerns/ManagesRateLimit.php:16-50`); ig-publish also lists 80004. OpenPost maps
    4, 17, 80001, 80002 and 368/1390008 (`errors.go:164-190`).
- **Headers:** `X-App-Usage` (`call_count`, `total_cputime`, `total_time` as %) and `X-Business-Use-Case-Usage`
  (`{id: [{type, call_count, total_cputime, total_time, estimated_time_to_regain_access}]}`). Pages may also send
  `X-Page-Usage` (UNVERIFIED).
- **Decision:** parse the headers on every response and pause non-urgent jobs (analytics, comments) above 80%.
  ig-publish stops at 85% (`docs/instagram-api.md:77`).
- **Publishing quotas:** IG 100 posts / 24 h (`content_publishing_limit`); FB Reels 30 / 24 h per Page.
- **Spam heuristics:** Meta publishes no thresholds. ig-publish defaults to at most 10 posts per 3 h and at least
  30 min between Reels. Error 368/1390008 ("temporarily blocked") and 2207051 mean slow down; ig-publish
  holds for 24 h.

### 7.2 Error map (code / subcode → action)

| Code (subcode) | Meaning | Action |
|---|---|---|
| 190 (463 expired, 467 invalid, 460 password changed, 458 app removed) | Token invalid | Mark the account "Reconectar" |
| 190 (459) / message "You cannot access the app till you log in…" | Account checkpoint | Ask the user to log in on facebook.com or instagram.com, then reconnect; a refresh won't help (OpenPost `errors.go:165-168`, Postiz IG `:342-354`) |
| 190 (492) | User lost the Page role | Reconnect after an admin restores the role |
| 10, 200, "(#200)" | Missing permission or Page task | Reconnect and grant everything; check that `tasks` include `CREATE_CONTENT` |
| 100 (33) | Object does not exist (Page or post deleted) | Terminal |
| 100 "valid insights metric" | Deprecated metric | Drop the metric and retry the others |
| 4, 17, 32, 613, 80000–80009, 80014, HTTP 429 | Rate limit (80001 Pages, 80002 Instagram; the rest are other BUC families) | Back off (`estimated_time_to_regain_access`) |
| 368 (1390008) | Temporarily blocked (posting too fast) | Back off, retry later |
| 1, 2, HTTP 5xx, "Sorry, something went wrong", non-JSON body | Transient or unknown | Read state before any retry; never blind-retry a publish |
| 9007 (2207027) | IG media not ready | Wait and poll again |
| 9004 / 2207052, 2207003, 2207077 | IG could not fetch or download media | Check the public URL; new container |
| 2207006, 2207008, 2207020, 2207032, 2207053 | Container unusable or expired (2207008: retry 1–2× within 30 s–2 min) | New container (ig-publish `publisher.py:47-50`) |
| 2207004, 2207005, 2207009, 36001, 36003, 2207026, 2207085, 2207057, 2207010, 2207028 | IG media or caption validation | Terminal: tell the user what to fix |
| 2207042 | IG publishing quota reached | Hold until the quota frees |
| 2207051, 2207001 | IG spam / restricted | Stop, 24 h hold |
| 2207050 | IG user restricted | Terminal |
| 2207018 / "param collaborators is not allowed" | Bad collaborator | Terminal |
| 2207078, 2207081 | Trial Reel limit / not supported | Publish as a normal Reel |
| 2207082 | Audio processing (silent track) | Retry with the video volume set to 0 |
| 1772107 (code 20) | IG comment error; Brightbean says it comes **after** creation, Postiz says the comment was **not** added | Look for the comment; if absent, retry once without `fields` (§4.5) |
| 1366046, 2069019 | FB photo too big or bad, or invalid file | Re-encode |
| 1366051 | FB duplicate photos | Terminal |
| 1346003, 1404102 | FB content flagged | Terminal |
| 1404078 | Page Publishing Authorization required | User action on Facebook |
| 4854002 / 1404006 / 1404112 | Identity or security check | User action |
| 1609005 | Link could not be scraped | Retry without `link` |
| 1609008 | Cannot post facebook.com links | Terminal |
| "Unpublished posts must be posted to a page as the page itself" | User token used instead of the Page token | Fix the token, reconnect |

Sources: Postiz `facebook.provider.ts:62-287` and `instagram.provider.ts:102-447`; OpenPost
`errors.go:104-185` and `instagram.go:727-790`; ig-publish `publisher.py:42-53`, `hints.py:40-80` and
`docs/instagram-api.md:79-96`; Brightbean `meta_insights.py:13-67`.

---

## 8. Concrete implications for Planificador de Redes

1. **Settings → Conexiones → Meta** (Spanish UI). Fields:
   - App ID, App Secret, optional `config_id`, Graph version (`v25.0`), callback port, optional HTTPS callback
     port.
   - For Instagram Login: IG App ID and IG App Secret.
   - Public media mode: "Túnel temporal (Cloudflare)" / "URL pública propia" / "Desactivado".
   - The screen shows the exact redirect URIs to paste into the Meta dashboard and a step-by-step guide: create a
     Business app → Facebook Login for Business + Instagram (API setup with Facebook login) → redirect URI →
     connect in Development → switch to Live.
2. **Provider modules** (`meta_facebook.py`, `meta_instagram.py`, `meta_common.py`) on an `httpx` client that:
   - always sends the token as a query/form parameter for graph calls and `Authorization: OAuth` for rupload;
   - treats an HTTP 200 body with `error` as an error;
   - parses the usage headers;
   - classifies errors with the table in §7.
3. **Publishing state machine per (post, network)**, persisted after every step:
   - Facebook: `uploaded(video_id|photo_ids)` → `processing` → `publishing` → `published(post_id, permalink)`.
   - Instagram: `container_created(id)` → `uploaded` → `finished` → `publish_intent` → `published(media_id)`.
   - On restart or error: read the remote status first (§4.1, §3.3).
4. **Media pipeline (local):**
   - Pillow converts to JPEG sRGB and checks the aspect ratio (IG 4:5–1.91:1, auto-pad or crop option), ≤8 MB
     for IG and ≤4 MB for FB.
   - ffprobe/ffmpeg checks duration, codec, fps, bitrate and resolution against §3.3, §3.5 and §4.4, with an
     optional transcode. Whether to bundle ffmpeg on Windows is an open question.
5. **Public media server:** a separate tiny ASGI app on `127.0.0.1:{PUBPORT}` that serves only tokenised
   one-shot files. The `cloudflared` process is managed by the app (download it from GitHub releases on first
   use, with the user's consent). The tunnel is kept up only while IG containers are processing.
6. **Scheduler:**
   - Our jobs run every minute.
   - Pre-flight about 2 min before the due time: refresh the token if needed, check the IG
     `content_publishing_limit`, upload media.
   - Optional Facebook native scheduling (§3.7).
   - Daily jobs: `debug_token` check, IG-Login token refresh, analytics snapshot, Story insights at +23 h, and
     comment polling every 10–15 min.
7. **Account discovery:**
   - One OAuth click lists Pages and their linked IG accounts.
   - Accounts are rejected when they lack `CREATE_CONTENT`, or when an IG account is not Business and the user
     wants Stories.
   - An "Instagram sin Página" option uses Instagram Login.

---

## 9. Open questions and risks

1. **Live mode vs the `http://localhost` redirect.** Facebook Page posts are visible only to app roles in
   Development, so we must go Live, but Meta allows HTTP localhost only in Development. Needs a real test. The
   fallbacks are toggling modes for reconnects, or an HTTPS localhost callback with a self-signed certificate
   (also UNVERIFIED that Meta accepts `https://localhost`).
2. **Live mode with Standard Access for the app admin:** does it keep working without App Review and without
   Business verification in 2026? The Postiz docs say yes; Meta's App Review page is ambiguous. Which Basic
   settings are mandatory to go Live (privacy policy URL, data-deletion URL)? Where does a personal user host a
   privacy policy URL?
3. Do Instagram posts made through a Development-mode app appear publicly? ig-publish suggests yes; this is not
   documented.
4. Does `upload_type=resumable` work with Instagram Login tokens (`graph.instagram.com`)? Meta's v26 SDK lists
   the parameter for that API, ig-publish says it does not work. Does it accept **images**? Docs show video only.
5. **Cloudflare quick tunnel:**
   - Does Meta's fetcher reliably reach `*.trycloudflare.com`?
   - DNS propagation delay after start-up.
   - Quick-tunnel ToS ("testing").
   - Corporate firewalls or antivirus on the user's PC.
6. **Facebook Page `data_access_expires_at` (90 days):** does it ever block a Page token in practice, forcing a
   reconnect roughly every 90 days?
7. **Metric availability:** `page_video_views`, `page_views_total`, `page_daily_unfollows_unique`, IG
   `follower_count` on v25/v26, IG Story metrics, and the Dataslayer "2026-08-06" wave. Build discovery-tolerant
   code and verify against the user's real accounts.
8. **Large Facebook videos:** the 2026 limits for multipart `source` vs the Resumable Upload API, and whether the
   app-scoped upload needs the 60-day user token (and therefore periodic reconnects).
9. **Exact `scheduled_publish_time` windows** per endpoint: 30 days in the Posts guide vs 75 days / 6 months in
   the references.
10. **ffmpeg on Windows** for validation and transcoding (licence and size), and Story-image-to-video conversion.
11. **Limits that may have changed:** Instagram carousel maximum (10 in the API vs 20 in the app), collaborator
    maximum (3?), and image formats (PNG/WebP accepted in practice?).
12. **Account bans and spam:** automated posting bursts can trigger 368 or 2207051 holds. Keep conservative
    defaults (ig-publish's 10 posts / 3 h).
13. **Graph v26.0:** move the default once the checklist passes. Watch the 2026-10-27 all-version enforcement of
    the protocol deprecations.

---

## 10. Sources

**Reference code (read only, AGPL; nothing copied)**

- `postiz-app/libraries/nestjs-libraries/src/integrations/social/{facebook,instagram,instagram.standalone}.provider.ts`
  and `.../dtos/posts/providers-settings/{instagram,facebook}.dto.ts`
- `brightbean-studio/providers/{facebook,instagram,instagram_login,meta_oauth,meta_accounts,meta_insights,meta_comments}.py`,
  `apps/publisher/engine.py`, `apps/social_accounts/views.py`, `development_specs/meta-analytics-manual-tests.md`
- `openpost/apps/server/internal/platform/{facebook,instagram,analytics_meta,errors,link_cards}.go`,
  `openpost/apps/docs/content/docs/self-hosting/integrations/{facebook,instagram}.mdx`
- `gitroomhq/postiz-docs`: `self-host/providers/{facebook,instagram}.mdx`
- `deegitech/ig-publish` (MIT, Oct 2026): `src/ig_publish/{graph,publisher,media,hints}.py`,
  `docs/instagram-api.md`, `README.md`

**Meta and third-party pages (via web search)**

- Graph API versions and changelog: <https://developers.facebook.com/docs/graph-api/changelog/versions/>,
  <https://developers.facebook.com/docs/graph-api/changelog/version26.0/>
- Login security, Strict Mode, Enforce HTTPS: <https://developers.facebook.com/docs/facebook-login/security>,
  <https://developers.facebook.com/blog/post/2018/06/08/enforce-https-facebook-login/>
- Facebook Login for Business: <https://developers.facebook.com/docs/facebook-login/facebook-login-for-business/>
- Access tokens: <https://developers.facebook.com/docs/facebook-login/guides/access-tokens>
- App Review / Live mode: <https://developers.facebook.com/docs/resp-plat-initiatives/individual-processes/app-review/submission-guide>
- Pages posts and scheduling: <https://developers.facebook.com/docs/pages-api/posts>
- Page photos: <https://developers.facebook.com/docs/graph-api/reference/page/photos>
- Reels Publishing: <https://developers.facebook.com/docs/video-api/guides/reels-publishing>
- Video publishing / Resumable Upload: <https://developers.facebook.com/docs/video-api/guides/publishing/>,
  <https://developers.facebook.com/docs/graph-api/guides/upload>
- Page Stories: <https://developers.facebook.com/docs/page-stories-api>
- IG Content Publishing: <https://developers.facebook.com/docs/instagram-platform/content-publishing/>
- IG User Media: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/>
- IG Container: <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-container>
- IG Refresh Access Token: <https://developers.facebook.com/documentation/instagram-platform/reference/refresh_access_token>
- IG Account insights: <https://developers.facebook.com/docs/instagram-platform/api-reference/instagram-user/insights>
- IG Media insights: <https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights/>
- IG metrics blog (2026-04-22): <https://developers.facebook.com/blog/post/2026/04/22/instagram-api-updates-for-partnerships-metrics-collaboration-and-engagement/>
- Page Insights reference (90-day window, 2-year retention, June 2026 deprecations): <https://developers.facebook.com/docs/graph-api/reference/insights/>
- Page Insights updates (2025-08-15): <https://developers.facebook.com/blog/post/2025/08/15/page-insights-api-updates/>
- Rate limits: <https://developers.facebook.com/docs/graph-api/overview/rate-limiting/>
- Third-party cross-checks:
  - Emplifi: <https://docs.emplifi.io/platform/latest/home/facebook-metric-deprecation-june-2026>
  - Windsor: <https://windsor.ai/documentation/guide-for-deprecating-metrics-for-facebook-organic-connector-june-15-2026/>
  - Improvado: <https://improvado.io/product-updates/connectors/facebook-pages-metrics-deprecation>
  - Dataslayer: <https://support.dataslayer.ai/understanding-upcoming-removal-of-metrics-on-facebook>
  - Cloudflare quick tunnels: <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/>
  - Developer report that a dev-mode app makes posts invisible: <https://github.com/cobrasmashbc/Cobrasmash/issues/39>

**Added by the verification pass (§11)**

- Meta's official generated SDK `facebook-business` 26.0.2 (PyPI, 2026-09-21; read from the sdist, nothing
  executed): `facebook_business/apiconfig.py`, `adobjects/page.py`, `iguser.py`, `iguserforigonlyapi.py`,
  `instagraminsightsresult.py`, `igcomment.py`, `comment.py`, `contentpublishinglimitresponse.py`. Release dates:
  <https://pypi.org/pypi/facebook-business/json>.
- `inovector/mixpost` (MIT, PHP, last commit 2026-03-16): `src/SocialProviders/Meta/Concerns/{ManagesMetaResources,ManagesRateLimit,MetaOauth,ManagesFacebookOAuth}.php`,
  `src/Services/FacebookService.php`.
- `openpost/docs/research/provider-production-review-requirements-2026-09-19.md` (Meta app modes and Live
  requirements, citing Meta's "App modes" and "Basic settings" docs).
- cloudflared releases: `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe`
  redirects to release `2026.10.0`.

---

## 11. Verificación (adversarial pass, 2026-10-15)

Method: each claim was re-checked against a source **other than** the one the report cited (another reference
repo, Mixpost, Meta's generated SDK, GitHub/PyPI metadata). The search budget for this run was exhausted before
the pass began, so **no official Meta page could be read directly**; claims resting only on Meta docs remain
UNVERIFIED unless code or the SDK backs them.

| # | Claim | Verdict | Independent source |
|---|---|---|---|
| 1 | All references pin Graph `v25.0`; OpenPost has an env override | CONFIRMED | grep of the three repos; Mixpost also defaults to `v25.0` (`FacebookService.php:18`) |
| 2 | v26.0 is current; protocol features break on all versions on 2026-10-27 | PARTLY | v26 confirmed by Meta SDK 26.0.0 on PyPI (2026-08-06, `API_VERSION 'v26.0'`). 07-29 release day and 10-27 enforcement date UNVERIFIED |
| 3 | FB OAuth: `dialog/oauth` → `oauth/access_token` → `fb_exchange_token` → `/me/accounts` Page tokens | CONFIRMED | Mixpost `MetaOauth.php:17-37`, `ManagesFacebookOAuth.php:12-16`, `ManagesFacebookPageResources.php:36` |
| 4 | Reject Pages without the `CREATE_CONTENT` task | CONFIRMED (task name) | Meta SDK `page.py` task enum `CREATE_CONTENT` |
| 5 | Page tokens from a long-lived user token never expire | UNVERIFIED | Only Brightbean + Meta docs from memory. Postiz still stamps its FB integration with a 59-day expiry (`facebook.provider.ts:386`); plan for a reconnect prompt anyway |
| 6 | IG Login: `instagram.com/oauth/authorize`, multipart POST to `api.instagram.com/oauth/access_token`, `ig_exchange_token`, `ig_refresh_token` | CONFIRMED | Postiz `instagram.standalone.provider.ts:80-161` + Brightbean `instagram_login.py:54-281` + SDK `IG*AccessTokenForIGOnlyAPI` classes |
| 7 | `http://localhost` redirect allowed only in Development mode; IG Login needs HTTPS | UNVERIFIED (consistent) | Indirect: Postiz wraps redirects in `redirectmeto.com` only for IG Login/Threads, never for Facebook Login |
| 8 | Dev-mode FB Page posts are visible only to app roles → must go Live | UNVERIFIED | Only postiz-docs and one GitHub issue. OpenPost research confirms dev mode authorizes only app-role users, and that a role-only private app is exempt from business verification, but says nothing about post visibility |
| 9 | FB photos: multipart `source`, `published=false`, then `/feed` + `attached_media` | CONFIRMED | Mixpost `ManagesMetaResources.php:38-103`; SDK lists `temporary`, `attached_media` |
| 10 | FB Reels/video Stories: `upload_phase=start` → rupload `upload_url` with `Authorization: OAuth` → `finish` (+ `video_state`) | CONFIRMED | Postiz `facebook.provider.ts:579-596,723`; SDK `create_video_reel`/`create_video_story` enums. Local-bytes headers (`offset`, `file_size`) are used by no FB reference; same protocol as IG rupload (ig-publish), so likely but UNVERIFIED for FB |
| 11 | FB Reels 3–90 s, 30 per Page / 24 h | UNVERIFIED | No source besides Brightbean (cited) |
| 12 | FB `/scheduled_posts` edge and scheduling params | CONFIRMED (CORRECTED from UNVERIFIED) | SDK `page.py` `get_scheduled_posts`; windows (30 d / 75 d / 6 mo) stay UNVERIFIED |
| 13 | `/videos` on `graph-video.facebook.com`; legacy chunked flow "may still work" | CORRECTED | All four code refs use `graph.facebook.com`; Mixpost uses `start/transfer/finish` with the Page token |
| 14 | FB Stories cannot be natively scheduled | CORRECTED → UNVERIFIED | SDK lists `video_state=SCHEDULED` + `scheduled_publish_time` on `/video_stories` |
| 15 | IG container `status_code` values; read before retrying `media_publish` | CONFIRMED | Postiz `instagram.provider.ts:679-947`, OpenPost `instagram.go:683-1139`, Brightbean `instagram.py:427-452` |
| 16 | IG resumable upload works only with Facebook Login; IG Login has no audio | CORRECTED → disputed | SDK `iguserforigonlyapi.py:284-302` lists `upload_type`, `audio_configuration`, `audio_name` for IG Login; ig-publish says IG Login needs public URLs |
| 17 | IG has no native API scheduling | CONFIRMED | SDK `iguser.py` `create_media` has no publish-time param (only read-only `/scheduled_media`) |
| 18 | IG images 4:5–1.91:1; carousel 2–10; Stories have no caption; Stories Business-only | CONFIRMED | Postiz `:60,143-148,301,338,420`; OpenPost `instagram.go:358,443`, `errors.go:238,245`; Brightbean `instagram.py:335`. JPEG-only and 8 MB remain from Meta docs only (Brightbean and OpenPost also send PNG/WebP/GIF; keep converting to JPEG) |
| 19 | IG quota 100 posts / 24 h | CONFIRMED (read it live) | Brightbean `instagram.py:173-180`, ig-publish observation; Postiz/OpenPost "25" messages are stale text |
| 20 | Reels ≤300 MB, ≤1920 px, ≤25 Mbps; 3 s–15 min | PARTLY | Size/width/bitrate: Postiz `instagram.provider.ts:193`. Duration: ig-publish only (UNVERIFIED) |
| 21 | 1772107 means the comment was created → treat as success | CORRECTED | Postiz `:386-392` says the comment was **not** added; now "look up, then retry once" |
| 22 | Comment endpoints: FB `/{comment}/comments`, `is_hidden`; IG `/{comment}/replies`, `hide` | CONFIRMED | SDK `comment.py`, `igcomment.py:109-177` |
| 23 | FB metric names (`page_media_view`, `page_total_media_view_unique`, `page_follows`, `post_media_view`…) and the 2026-06-15 removals | CONFIRMED | Postiz `facebook.provider.ts:1035-1045,1105-1109`, OpenPost `analytics_meta.go:20-24,105-114`. 2025-11-15 date: UNVERIFIED |
| 24 | IG metric removals (impressions/plays/video_views) and 2026 additions | CONFIRMED | SDK `instagraminsightsresult.py:44-67` |
| 25 | Rate-limit codes 4/17/32/613/80001/80002 | CORRECTED (expanded) | Mixpost `ManagesRateLimit.php:16-50` (80000–80009, 80014; usage headers parsed) |
| 26 | `cloudflared` single Windows binary from GitHub releases; quick tunnel needs no account | CONFIRMED (binary) | GitHub release 2026.10.0 asset `cloudflared-windows-amd64.exe`; cloudflared README "TryCloudflare". Meta fetching from `trycloudflare.com` UNVERIFIED |
| 27 | `deegitech/ig-publish` exists, MIT, Oct 2026, and the cited lines say what the report says | CONFIRMED | `git ls-remote` (tag v0.1.0), LICENSE, last commit 2026-10-04; `docs/instagram-api.md:27-77` match |

**Highest remaining risks (test first with the user's real app):** #7 and #8 (localhost vs Live mode), #5 (Page
token lifetime), #16 (IG Login resumable upload), #11 and the native-scheduling windows, and the 2026-10-27 v26
protocol enforcement (#2).

# TikTok API facts used by RobloxAutoPromo

**Date checked:** 2026-09-24.

**How this was checked:** the build sandbox's egress proxy blocked
`developers.tiktok.com` (HTTP fetch refused), so the pages could not be read in
full. Each fact below comes from web-search excerpts of the official pages
listed with it. The code was written against those excerpts. Some items are
marked **UNVERIFIED**: they come from memory of the docs or from third-party
guides only. **Re-read the linked pages before you submit an audit or change any
constant in `app/tiktok.py`.**

## 1. Login Kit (OAuth v2)

| Fact | Status | Source |
|---|---|---|
| Authorize URL `https://www.tiktok.com/v2/auth/authorize/` with `client_key, response_type=code, scope (comma-separated), redirect_uri, state` | Endpoint from docs/migration notes; the path was not re-read this time | https://developers.tiktok.com/doc/login-kit-desktop/ , https://developers.tiktok.com/bulletin/migration-guidance-oauth-v1 |
| Token endpoint `POST https://open.tiktokapis.com/v2/oauth/token/`, form-encoded; `grant_type=authorization_code` (+ `code`, `redirect_uri`, `code_verifier`) or `grant_type=refresh_token` | Verified (search excerpt) | https://developers.tiktok.com/docs/en/oauth-user-access-token-management |
| Access token lifetime is **24 h** (`expires_in` 86400). Refresh token lifetime is **365 days** (`refresh_expires_in` 31536000). Refreshing needs no user consent. | Verified | same |
| **Desktop apps must use PKCE.** `code_verifier` is 43–128 chars from `[A-Za-z0-9-._~]`. Only `code_challenge_method=S256` is supported. The **desktop** docs hex-encode SHA-256 (`CryptoJS.SHA256(v).toString(Hex)`), which is not the RFC 7636 base64url. | Verified | https://developers.tiktok.com/doc/login-kit-desktop/ |
| Web Login Kit PKCE requirement | UNVERIFIED. We use the desktop flow. | https://developers.tiktok.com/docs/en/login-kit-overview |
| A `http://localhost:<port>/callback` redirect is accepted for desktop apps | **UNVERIFIED.** Register the exact URI in the portal. If it is rejected, set `TIKTOK_REDIRECT_URI`. | login-kit-desktop |

Implementation: `tiktok.authorize_interactive(settings)` builds the URL with a
random `state` and a PKCE verifier and opens the browser. A one-shot
`http.server` listener on `127.0.0.1:<port>` accepts only `/callback` and
checks `state`. The code is then exchanged for tokens, which go into
`app.secrets`. Set `[publish].pkce_encoding="base64url"` if TikTok changes the
encoding.

## 2. Scopes

| Scope | Used for | Needs app review? |
|---|---|---|
| `user.info.basic` | Identity (open_id) | Default Login Kit scope |
| `video.upload` | Upload to the creator's **inbox** as a draft; the creator finishes posting in the TikTok app | The scope must be added and approved on the app |
| `video.publish` | **Direct Post** to the profile | The scope must be approved. The client must pass an **audit** before posts can be anything other than private (see §4). |
| `video.list` | Display API `/v2/video/query/` metrics | The scope must be approved |

## 3. Content Posting API

Source: https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post ,
https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video ,
https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info ,
https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide ,
https://developers.tiktok.com/docs/en/content-posting-api-reference-get-video-status

- **Creator info:** `POST /v2/post/publish/creator_info/query/` returns
  `privacy_level_options`, `comment_disabled`, `duet_disabled`,
  `stitch_disabled` and `max_video_post_duration_sec`. You **must** call it
  before a Direct Post, and the UI's privacy options must come from
  `privacy_level_options`. (Verified.)
- **Direct Post init:** `POST /v2/post/publish/video/init/` with a `post_info`
  body (`title`, `privacy_level`, `disable_comment`, `disable_duet`,
  `disable_stitch`, `video_cover_timestamp_ms`, `brand_content_toggle`,
  `brand_organic_toggle`, `is_aigc`) and a `source_info` body (`source` =
  `FILE_UPLOAD` | `PULL_FROM_URL`, `video_size`, `chunk_size`,
  `total_chunk_count` | `video_url`). The response returns `publish_id` and,
  for FILE_UPLOAD, `upload_url`. The field names were confirmed by search
  excerpts and the existence of `is_aigc` and the brand toggles is verified.
  The exact field list was not re-read in full (**partially verified**).
- **Inbox upload init:** `POST /v2/post/publish/inbox/video/init/` with
  `source_info` only. The creator gets an inbox notification and finishes
  editing and posting in the app. **At most 5 pending shares in any 24 h**;
  going over returns `spam_risk_too_many_pending_share`. (Verified.)
- **FILE_UPLOAD chunking** (verified):
  - Each chunk except the last is between **5 MB and 64 MB**.
  - The last chunk may be larger than `chunk_size`, up to **128 MB**, so it can
    take the trailing bytes.
  - **At most 1000 chunks** per file.
  - Files **under 5 MB** are sent whole, with `chunk_size = video_size`.
  - Chunks are sent as `PUT upload_url` requests with
    `Content-Range: bytes first-last/total` and `Content-Type: video/mp4`.
  - `total_chunk_count = floor(video_size / chunk_size)`. **Partially
    verified:** the floor rule follows from "the last chunk absorbs trailing
    bytes".
  - The upload URL expires about 1 h after init. **UNVERIFIED.**
- **PULL_FROM_URL** needs the URL's domain or URL prefix to be verified in
  the developer portal. **Not verified this session.** We don't use it,
  because the app runs locally and has no public host.
- **Status:** `POST /v2/post/publish/status/fetch/` with `{publish_id}`
  returns one of `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`,
  `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE` or `FAILED`, plus `fail_reason` and
  `publicaly_available_post_id` (TikTok's spelling). Moderation usually takes
  under a minute but can take hours. **Only `PUBLISH_COMPLETE` is treated as
  published.** (Verified.)
- **Scheduling:** TikTok has **no native scheduling or `schedule_time`**. The
  API posts immediately, so scheduling happens in our client (`app/scheduler.py`).
  This is verified only by third-party guides. No official field was found.

## 4. Unaudited clients, audit and UX rules

Source: https://developers.tiktok.com/docs/en/content-posting-api-get-started ,
https://developers.tiktok.com/docs/en/content-sharing-guidelines

- Content from an **unaudited client can only be posted `SELF_ONLY`**
  (private). (Verified.)
- An unaudited client can post for **at most 5 users per 24 h**. Those users'
  accounts **must be private** when posting. (Verified.)
- The limits are lifted only after TikTok **audits** the client for ToS
  compliance. (Verified.) Our code enforces this: `direct` mode is refused
  unless `[publish].app_audited = true` or the state key
  `tiktok_app_audited=1` is set.
- UX rules for audited apps (verified):
  - Show a **preview** of the content.
  - The user must pick **privacy manually, with no default**.
  - **Interaction toggles** (comment, duet, stitch) must be off by default and
    turned on by the user.
  - Show the consent text *"By posting, you agree to TikTok's Music Usage
    Confirmation"* before the publish button.
  - Upload content only after the user **expressly consents**.
  - Commercial content must be disclosed with **Content Disclosure**
    (`brand_organic_toggle` for "Your brand", `brand_content_toggle` for
    "Branded content / paid partnership").
  - AI-generated content can be labelled with `is_aigc`.

  Our code requires a per-post approval record (`approved:<post_id>`, which may
  hold JSON `{privacy_level, disable_*, is_aigc, brand_*}`). A privacy level
  missing from the creator's options is refused. Settings the creator has
  disabled are forced off. **The UI (OPS agent) is responsible for the preview
  and the consent text.**
- **Rate limits:** requests use a 1-minute sliding window, and going over
  returns HTTP 429 `rate_limit_exceeded` (verified:
  https://developers.tiktok.com/doc/tiktok-api-v2-rate-limit). The per-endpoint
  numbers come from third-party guides only (**UNVERIFIED**): init is about 6
  per minute per token, status fetch about 30 per minute per token, and daily
  post caps are about 15–25 per creator, shared across all clients. We use 2
  posts per day by default and pause on any 429 or `spam_risk_*` response.

## 5. Display API (metrics)

Source: https://developers.tiktok.com/doc/tiktok-api-v2-video-query ,
https://developers.tiktok.com/docs/en/display-api-overview

- `POST /v2/video/query/?fields=...` with `{"filters":{"video_ids":[...]}}`
  accepts **up to 20 IDs** and only returns the authorised user's own videos.
  It needs the `video.list` scope. (Verified.)
- Available fields include `view_count`, `like_count`, `comment_count`,
  `share_count`, `share_url`, `create_time`, `duration` and `title`.
  (Verified.)
- **Average watch time, completion rate, profile visits and traffic sources
  are NOT available** through this API. Those columns stay **NULL** for
  `source='api'` rows. You can enter them by hand from TikTok Studio as
  `source='manual'`. We never estimate values.

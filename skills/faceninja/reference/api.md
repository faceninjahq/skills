# FaceNinja API: compact notes for agents

Base URL `https://faceninja.net` (or `$FACENINJA_API_BASE`). Every call except the device sign-in sends
`Authorization: Bearer fn_live_...`. JSON in, JSON out. The full reference is https://faceninja.net/docs/ and the
OpenAPI 3.1 file https://faceninja.net/docs/openapi.yaml. `scripts/faceninja.py` wraps all of this; read on only for
what it does not cover.

## Workflow

```
POST /v1/photos?filename=me.jpg   body = the image bytes, Content-Type: image/jpeg   -> 201 {"id": "ph_..."}
POST /v1/heads                    {"photo_ids": ["ph_..."], "name": "Ana",
                                   "attestation": {"rights_and_consent": true, "adult": true}}   -> 202 {"id": "hd_...", "status": "queued"}
                                  header Idempotency-Key: <uuid>  (a retry returns the same head, no second charge)
GET  /v1/heads/{id}?wait=60       holds up to 60 s; loop until status is completed | failed | deleted;
                                  stop on needs_review | needs_licence and send the user to
                                  https://faceninja.net/factory#/heads/{id} (staff review)
POST /v1/heads/{id}/files         {"format": "glb"}  -> {"url", "filename", "bytes", "sha256"}; the url works for 60 s
GET  <url>                        download, without the API key
```

Free sample heads (no credits, any account): `GET /v1/samples` lists them with each file's `bytes` and `sha256` (no key
needed to list); `GET /v1/samples/{id}/files/{glb|fbx|metahuman_zip}` returns a link that works for 5 minutes (`?redirect=1`
for a 302) and needs an account. 30 sample files a day per account (`429 sample_limit`). Licence: /samples/LICENSE.txt.

Test mode: base URL `https://sandbox.faceninja.net`, the same API with Paddle's test cards (4242 4242 4242 4242, any future
expiry, any security code) and no real heads: each head gets a sample head's files after about a minute (`test_build` in the
head; a first photo named `fail-<code>.jpg` fails with that code). Every answer has `FaceNinja-Test-Mode: true` and
`"test_mode": true`. Accounts and keys are separate from production and deleted 30 days after sign-up. Details:
https://faceninja.net/docs/#test-mode

Statuses: `queued` (waiting in line; `queue.position`, `queue.eta_s`) → `processing` (`progress` 0–100; `stage` and `message`
for display: `preparing`, `building`, `finishing`) → `completed` or `failed` (`error.code`, `error.message`; the credits are
returned). `DELETE /v1/heads/{id}` before it finishes also returns the credits.

Holds that need a person: `needs_review` (every head made from uploaded photos, before 3D work) and `needs_licence` (a
likely public figure), both with stage `licence_review` and a `review` object (`required`: `staff` or `licence`,
`expires_at`). Review usually takes 1–2 business days, then the head goes back to `queued` and builds in 10–20 minutes.
`?wait` returns at once on a hold and no webhook fires: stop polling and send the user to the Factory link.

Formats for `files`: `glb`, `fbx` (both with `arkit: true|false`), `obj`, `stl`, `ply`, `metahuman_zip`. Optional download
options: `texture` (jpg|png|none), `normal_map` (opengl|directx), `texture_size` (4k|2k|1k), `cornea`, `scale` (standard life size | measured); a new
option set answers `202` while it is prepared once (pass `wait` up to 25).

Formats made on request: a build delivers the GLB and FBX (in the head's ARKit setting) and the MetaHuman ZIP. OBJ, STL,
PLY and GLB/FBX without ARKit are made from the GLB the first time they are asked for: `202 {"status": "preparing",
"eta_s"}` with `Retry-After`; ask again until you get the link (`faceninja.py download` does). The head's `downloads`
array shows each format as `ready`, `preparing`, `on_request` or `expired`.

## Attestation and terms (required)

Every `POST /v1/heads` carries `"attestation": {"rights_and_consent": true, "adult": true}`: the user who uploads the photos
confirms that they have the right to use them and the consent of the person shown, that the person is 18 or older, and
that they follow the Content & Likeness Policy (https://faceninja.net/legal/content-policy). Without it: `400
attestation_required` (with `docs_url`), nothing is created. Ask the user; never send it on their behalf without asking
(`faceninja.py attest` asks once per computer). In multipart requests send it as the field `attestation` (the same JSON)
or as `rights_and_consent=true` and `adult=true`. `POST /v1/account/attestation {"attestation": {...}, "client": {"agent",
"host", "version"}}` records that the user confirmed it on a computer. An account that has not accepted the current
terms gets `403 terms_not_accepted`: the user accepts them once in Factory (https://faceninja.net/factory).

## Money

`GET /v1/account` → `credits`, `held_credits`, `prices.head` (1500), `prices.credits_per_usd` (100). A head's price is held
when it is created, kept when it completes, returned when it fails. `POST /v1/heads?dry_run=true` validates and quotes
without creating anything. `402 insufficient_credits` means nothing was created. Credits are bought in Factory only,
from launch day; until launch, sign-up on faceninja.net is by invitation (support@faceninja.net).

## Errors and limits

`{"error": {"code", "message", "field"?, "hint"?, "request_id"}}`; branch on `code`. A failed head's `error.code` is `no_face`,
`multiple_people`, `not_same_person`, `unreadable_photo`, `greyscale_photo`, `blurry_photo`, `face_too_small`, `content_rejected`, `build_failed` or `timeout`; after a review hold also `licence_rejected` (staff could not verify permission), `review_expired` (14 days passed) or `licence_withdrawn` (the evidence was deleted before delivery). A `503 busy` on create means nothing was made or charged: try again in an hour. 600 requests a minute per account,
30 head creations a minute, 20 file links a minute per head; `429` carries `Retry-After`. Photos: JPEG, PNG, WebP,
HEIC/HEIF, AVIF, 1 KB to 25 MiB, 1 to 15 per head, one person, unused photos expire after 24 h.

Retention (`retention` in the head): photos are deleted 7 days after the head finishes (`DELETE /v1/heads/{id}/photos`
sooner); download files after 30 days; the master they are rebuilt from after 1 year. After 30 days a file request (or
`POST /v1/heads/{id}/redownload`) rebuilds the files from the master: free, `202` while it is made, at most 3 times a day
per head (`429 redownload_limited`). After a year: `410 files_expired`.

## Skill versions and guidance for agents

Every `/v1` answer carries `FaceNinja-Skill-Latest` (the newest skill), `FaceNinja-Skill-Min` and `FaceNinja-Guidance`
(the revision of the guidance below). A client that sends `FaceNinja-Skill-Version` (or a `faceninja-skill/x.y.z`
User-Agent) older than the minimum gets `426 skill_update_required`; other clients are never refused.
`GET /v1/agent/guidance` (no key, cacheable) returns `{"rev", "skill": {"latest_version", "min_version"}, "limits":
{"max_heads_without_asking", "recommended_formats", ...}, "notices": [{"id", "kind", "text", "starts_at"?, "ends_at"?}]}`;
`kind` is `rule`, `notice`, `deprecation` or `maintenance`. `/skill/version.json` lists every skill file with its SHA-256.

## Device sign-in (how `faceninja.py login` gets a key)

```
POST /v1/device/code   {"client_name": "Claude Code on host", "client": {"agent", "host", "os", "version"}}
  -> {"device_code", "user_code": "WXZB-2345", "verification_uri", "verification_uri_complete", "expires_in": 600, "interval": 5}
POST /v1/device/token  {"device_code"}   every `interval` seconds
  -> 400 authorization_pending | slow_down (add 5 s to the interval) | expired_token | access_denied | invalid_grant
  -> 200 {"access_token": "fn_live_...", "key_id", "key_prefix", "key_name", "email"}   (once; store it, never print it)
DELETE /v1/keys/current   revokes the key that sends it
```

The person opens `verification_uri_complete`, signs in to FaceNinja and clicks Approve or Deny. A code works once and
expires after 10 minutes.

## Endpoints

<!-- endpoints:start -->
| Method | Path | What it does |
| --- | --- | --- |
| `GET` | `/v1/account` | Read the account |
| `POST` | `/v1/account/attestation` | Record the user's attestation for a computer |
| `GET` | `/v1/account/auto-reload` | Read the auto-reload settings |
| `PUT` | `/v1/account/auto-reload` | Change or turn off auto-reload |
| `GET` | `/api/status` | Read the service status |
| `POST` | `/v1/photos` | Upload one photo |
| `POST` | `/v1/photos/uploads` | Get direct upload links |
| `GET` | `/v1/photos/{id}` | Read a photo |
| `DELETE` | `/v1/photos/{id}` | Delete an unused photo |
| `GET` | `/v1/heads` | List heads |
| `POST` | `/v1/heads` | Create a head |
| `GET` | `/v1/heads/{id}` | Read a head, optionally waiting |
| `DELETE` | `/v1/heads/{id}` | Delete a head |
| `DELETE` | `/v1/heads/{id}/photos` | Delete a head's photos now |
| `DELETE` | `/v1/heads/{id}/improvement` | Withdraw a head's opt-in for improvement |
| `POST` | `/v1/heads/{id}/redownload` | Download a head again after its files expired |
| `GET` | `/v1/heads/{id}/events` | Follow a head (Server-Sent Events) |
| `GET` | `/v1/heads/{id}/files/{kind}` | Get a download link |
| `POST` | `/v1/heads/{id}/files` | Get a download link (body form) |
| `GET` | `/v1/samples` | List the free sample heads |
| `GET` | `/v1/samples/{id}/files/{kind}` | Get a download link for a sample file |
| `GET` | `/v1/webhooks` | List webhook endpoints and recent deliveries |
| `POST` | `/v1/webhooks` | Add a webhook endpoint |
| `DELETE` | `/v1/webhooks/{id}` | Remove a webhook endpoint |
| `GET` | `/v1/agent/guidance` | Current guidance for AI agents |
| `POST` | `/v1/device/code` | Start a device sign-in |
| `POST` | `/v1/device/token` | Collect the key of an approved sign-in |
| `DELETE` | `/v1/keys/current` | Revoke the key that sends this request |
<!-- endpoints:end -->

Staff review can interrupt the workflow: `needs_licence` or `needs_review` requires input, with credits held. Stop polling and follow https://faceninja.net/docs/#likeness-review.

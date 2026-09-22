# socialq — implementation plan

A standalone service that takes finished content and guarantees it gets posted.

This document assumes no context. Read it end to end before writing code; the
constraints in §2 and §9 are the ones that will bite, and several are
non-obvious.

---

## 1. What this is, and what produces its input

`video-maker` (a separate repo) renders short-form video and image content for a
product. It does not post anything. Each finished piece lands in `output/` as
media plus a JSON **Post sidecar**:

```json
{
  "product":  "deploysafe",
  "pipeline": "brainrot",
  "id":       "api-no-auth",
  "media":    ["output/brainrot/api-no-auth.mp4"],
  "caption":  { "default": "your frontend enforces the login. your api doesnt." },
  "surface":  { "instagram": "post" },
  "cta":      { "instagram": "link in bio" },
  "hashtags": { "instagram": ["#vibecoding", "#buildinpublic"] },
  "aigc":     true,
  "link":     null,
  "first_comment": null
}
```

Field meanings, because several are load-bearing:

| field | meaning |
|---|---|
| `product` | tenant. Maps 1:1 to a socialq project. |
| `pipeline` | which generator made it (brainrot, meme, story, stats). |
| `media` | **repo-relative** paths. Resolve against the producer's checkout. |
| `caption` | per-platform text, falling back to the `default` key. |
| `surface` | per-platform: `post` or `story`. A story is a different API call. |
| `cta` | fixed line appended after the caption. Configuration, never model output. |
| `hashtags` | fixed set appended after the CTA. Same. |
| `aigc` | synthetic voice or generated imagery. TikTok requires disclosure. |

**Assembly rule.** The text posted to a platform is three blocks, blank line
between each, empty blocks dropped:

```
[caption]

[cta]

[hashtags]
```

`video-maker` exposes this as `Post.text_for(platform)`. socialq must produce
byte-identical output; port the function rather than reimplementing it.

**A `surface: "story"` target has empty `cta` and `hashtags` by design** —
Instagram accepts no text for a story through the API, so everything the viewer
reads is already in the image.

---

## 2. Constraints discovered the hard way

Do not re-derive these. Each one cost real time or money to establish.

**TikTok's own API is effectively closed to a script.** Direct Post requires an
audit on top of app review, and the audit requires a demo video of a UI showing
the creator a privacy selector, comment/duet/stitch toggles, commercial-content
disclosure and a music confirmation. A headless job has no UI to film. An
unaudited client is refused outright: the API returns
`unaudited_client_can_only_post_to_private_accounts` — even for
`privacy_level: SELF_ONLY`, because the restriction is on the *account* being
public, not on the requested privacy. **TikTok therefore goes through PostPeer**,
whose client is pre-approved for Direct Post.

**Instagram does not need any of that**, because posting to your own account
keeps the Meta app in development mode: no app review, no demo video, no
business verification. Review is only required for *other users'* accounts.

**PostPeer is not cheap, and credits are the binding constraint.** The free
tier is 20 credits/month. The cheapest paid plan is **$33/month** for 2,000
credits; there is nothing between those two. Per-credit rates quoted in their
marketing ($8.50/1,000 and similar) are marginal rates on plans that start at
$33, not something purchasable on its own.

Costs are 1 credit per post on most platforms, **5 for X, and 50 for an X post
containing a URL**. One X-with-link post is therefore two and a half months of
the free tier. **Never route X through PostPeer.**

**Credit exhaustion is handled by rotating between several free accounts.**
Three PostPeer accounts give 60 credits/month at no cost, which covers the
expected volume. This is a first-class requirement, not a workaround bolted on
later: see §8.2.1. Their terms were checked and contain no clause against
multiple accounts, but there is equally no clause protecting them -- an account
could be closed at any time, so nothing may break when one disappears.

**PostPeer's media library holds 10 items and has no delete endpoint** (verified
against their OpenAPI spec — only `POST /v1/media/upload` exists). Cleanup is
dashboard-only. Therefore **host media yourself and pass your own URLs**;
`mediaItems[].url` accepts any public URL.

**No music selection on any platform via API.** Instagram's docs are explicit:
a track cannot be chosen from its library programmatically, so audio must
already be in the video file. TikTok is the same for video. The one exception:
TikTok photo carousels get background music automatically via `autoAddMusic`
(default true), chosen by TikTok.

**Instagram Stories support no stickers at all** through the API — no music,
polls, questions, links or mentions.

---

## 3. Architecture

```
producers ──► SDK ──► Postgres ◄── worker ──► publishers ──► platforms
(video-maker)                         │
                                      └──► R2 (media)
```

Four layers, each aware only of the one below:

- **SDK** — what producers import. Uploads media, writes rows. One function.
- **Postgres** — projects, accounts, media, posts, targets, attempts.
- **Worker** — claims due work, publishes, records, retries, reconciles.
- **Publishers** — one class per platform. Swappable.

**The publisher boundary is the point of the design.** PostPeer must be a detail
of one file, not an architectural commitment. The day a native TikTok client
gets audited, that is a new `Publisher` subclass and a one-line routing change;
the queue, worker and SDK never learn it happened.

---

## 4. Data model

```sql
CREATE TABLE projects (
  id          TEXT PRIMARY KEY,           -- "deploysafe"
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
  id          BIGSERIAL PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  name        TEXT NOT NULL,              -- "deployedunsafe"
  platform    TEXT NOT NULL,              -- instagram | tiktok | x | linkedin
  handle      TEXT NOT NULL,
  publisher   TEXT NOT NULL,              -- "postpeer" | "instagram_graph"
  external_id TEXT,                       -- publisher's account id
  enabled     BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (project_id, name, platform)
);

CREATE TABLE media (
  id          BIGSERIAL PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  sha256      TEXT NOT NULL,
  url         TEXT NOT NULL,              -- public R2 url
  mime        TEXT NOT NULL,
  bytes       BIGINT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, sha256)             -- content addressed: never re-upload
);

CREATE TABLE posts (
  id            BIGSERIAL PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id),
  external_id   TEXT NOT NULL,            -- the sidecar's "id"
  pipeline      TEXT NOT NULL,
  caption       JSONB NOT NULL,
  cta           JSONB NOT NULL DEFAULT '{}',
  hashtags      JSONB NOT NULL DEFAULT '{}',
  media_ids     BIGINT[] NOT NULL,
  aigc          BOOLEAN NOT NULL DEFAULT false,
  scheduled_for TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, pipeline, external_id)   -- enqueue is idempotent
);

CREATE TYPE target_state AS ENUM
  ('pending','claimed','in_flight','published','failed','dead');

CREATE TABLE targets (
  id               BIGSERIAL PRIMARY KEY,
  post_id          BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  account_id       BIGINT NOT NULL REFERENCES accounts(id),
  surface          TEXT NOT NULL DEFAULT 'post',   -- post | story
  state            target_state NOT NULL DEFAULT 'pending',
  attempts         INT NOT NULL DEFAULT 0,
  next_attempt_at  TIMESTAMPTZ,
  claimed_at       TIMESTAMPTZ,
  claimed_by       TEXT,
  provider_post_id TEXT,
  permalink        TEXT,
  last_error       TEXT,
  UNIQUE (post_id, account_id, surface)
);

CREATE TABLE attempts (
  id           BIGSERIAL PRIMARY KEY,
  target_id    BIGINT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  n            INT NOT NULL,
  idem_key     TEXT NOT NULL UNIQUE,      -- "target:<id>:<n>"
  request      JSONB,
  response     JSONB,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  UNIQUE (target_id, n)
);

CREATE INDEX ON targets (state, next_attempt_at);
CREATE INDEX ON posts (project_id, scheduled_for);
```

**`targets` is the unit of work** — one row per (post, account, surface). A
TikTok success and an Instagram failure are independent rows, so retry is
naturally per-platform. Everything carries `project_id` from the first commit,
so a second product is an INSERT rather than a migration.

---

## 5. Claiming work

Postgres is the queue. No Redis: `FOR UPDATE SKIP LOCKED` is a correct
claim-and-lock at orders of magnitude beyond this workload, and it keeps the
history in the same transaction as the work.

```sql
UPDATE targets t SET state = 'claimed', claimed_at = now(), claimed_by = $1
WHERE t.id IN (
  SELECT t2.id
  FROM targets t2
  JOIN posts p ON p.id = t2.post_id
  JOIN accounts a ON a.id = t2.account_id
  WHERE t2.state = 'pending'
    AND a.enabled
    AND p.scheduled_for <= now()
    AND (t2.next_attempt_at IS NULL OR t2.next_attempt_at <= now())
  ORDER BY p.scheduled_for
  FOR UPDATE SKIP LOCKED
  LIMIT $2
)
RETURNING t.*;
```

Scaling out is running more workers. No coordination.

---

## 6. Publishing one target

```
1. claim                    state -> claimed
2. INSERT attempts row      idem_key = target:<id>:<n>, COMMIT
   state -> in_flight       (committed BEFORE the network call)
3. call the publisher
4. on success: record provider_post_id + permalink, state -> published
   on failure: state -> pending, attempts += 1,
               next_attempt_at = now() + backoff(attempts)
               after N attempts -> dead, and alert
```

Step 2 committing before step 3 is what makes recovery possible. Without it a
crash between call and record is indistinguishable from a call that never
happened.

**Backoff**: exponential with jitter, e.g. `min(2^n minutes, 6h)`, cap at 6
attempts, then `dead`.

---

## 7. Reconciliation — the part that makes "no manual steps" true

A worker that dies mid-publish leaves an `in_flight` row. Retrying blindly
double-posts. Asking a human defeats the purpose. So **query the platform**:

On startup, and periodically, for every `in_flight` target older than ~30
minutes:

- **PostPeer**: `GET /v1/posts/` and match on the media URL within the window.
- **Instagram**: `GET /{ig-user-id}/media` and match recent media.

Found → record it as published. Not found → return to `pending` and retry.

This is code, not a notification. Do not ship a version that emails you about it.

---

## 8. Publishers

### 8.1 Interface

```python
class Publisher(Protocol):
    name: str
    def publish(self, target: Target, post: Post, media: list[Media]) -> Result: ...
    def find_recent(self, account: Account, since: datetime) -> list[RemotePost]: ...
```

`find_recent` exists for §7. A publisher without it cannot be reconciled.

### 8.2 PostPeer — TikTok

Base `https://api.postpeer.dev/v1`, header `x-access-key: $POSTPEER_API_KEY`.

```http
POST /v1/posts
{
  "content": "<text_for(platform)>",
  "mediaItems": [{ "type": "video", "url": "https://media.../x.mp4" }],
  "platforms": [{
    "platform": "tiktok",
    "accountId": "<from GET /v1/connect/integrations>",
    "platformSpecificData": {
      "privacyLevel": "PUBLIC_TO_EVERYONE",
      "disableComment": false,
      "disableDuet": false,
      "disableStitch": false,
      "isAigc": true,
      "draft": false
    }
  }],
  "publishNow": true
}
```

- `draft: false` (the default) publishes via DIRECT_POST. Their client is
  pre-approved, so posts are public.
- **`isAigc` must be set from `post.aigc`.** Synthetic voices require disclosure
  and undisclosed AI content is a takedown risk.
- Photo carousels: `mediaItems` of `type: "image"`, 1–32 of them, aspect ratio
  between 1:2.13 and 2.13:1. `content` is the **title, 90 chars**; `description`
  is separate at 4,000. Also `photoCoverIndex` and `autoAddMusic`.
- Useful endpoints: `GET /v1/usage/` (credit balance), `GET /v1/posts/{id}`,
  `POST /v1/posts/{id}/retry`, `GET /v1/tiktok/creator-info`.

#### 8.2.1 Key rotation — required, not optional

The free tier is 20 credits/month, so a single account runs out. Three accounts
are held and rotated.

**The credential is a pair, not a key.** Each PostPeer account connects its own
TikTok integration, so the `accountId` passed in `platforms[].accountId` belongs
to that account and is meaningless to the others. Swapping the API key without
swapping the account id will fail or, worse, post somewhere unintended.

```sql
CREATE TABLE publisher_credentials (
  id             BIGSERIAL PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id),
  publisher      TEXT NOT NULL,          -- "postpeer"
  label          TEXT NOT NULL,          -- which of the three
  api_key_ref    TEXT NOT NULL,          -- secret store key, never the secret
  account_ids    JSONB NOT NULL,         -- platform -> that account's id
  credits_left   INT,                    -- last seen, from GET /v1/usage/
  checked_at     TIMESTAMPTZ,
  enabled        BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (project_id, publisher, label)
);
```

Selection, per publish:

1. Refresh `credits_left` from `GET /v1/usage/` if `checked_at` is stale
   (> ~15 min). Do not call it on every publish; it costs 1 credit itself.
2. Pick the enabled credential with the **most** credits remaining. Most, not
   first-with-any: it spreads load and leaves headroom for retries.
3. If every credential is exhausted, do **not** fail the target. Set
   `next_attempt_at` to the start of the next month and leave it `pending`.
   Running out of credit is a wait, not an error.
4. Record which credential was used on the `attempts` row. Reconciliation (§7)
   must query the *same* account, since another one cannot see that post.

Failure modes to handle explicitly:

- **A credential is revoked or the account is closed.** Mark it `enabled =
  false` on an auth error and carry on with the others. One dead account must
  never stall the queue.
- **Credits reported stale.** A publish can still fail for insufficient credit
  after the check. Treat that as "try the next credential", not as a retry of
  the same one.
- **All three exhausted mid-batch.** Expected, not exceptional. See 3.

### 8.3 Instagram Graph — direct, no vendor

Use **Instagram API with Instagram Login** (not the Facebook-Login route).
Scopes `instagram_business_basic`, `instagram_business_content_publish`. The
account must be Business or Creator.

Three steps, and the polling is not optional:

```
POST /{ig-user-id}/media           -> creation_id     (container)
GET  /{creation_id}?fields=status_code   until FINISHED
POST /{ig-user-id}/media_publish   -> media id
```

- Media by **public URL** (`video_url` / `image_url`), which R2 provides. The
  resumable upload endpoint exists but is unnecessary.
- **Stories**: same flow with `media_type=STORIES`. No caption, no hashtags.
- Limit: **50 posts per 24h**.
- **Tokens**: long-lived, 60 days, refreshable. Refresh on a monthly schedule
  and write the new value back. A token must be ≥24h old to refresh, and one
  allowed to lapse past 60 days requires a manual browser round trip.

### 8.4 Routing

`accounts.publisher` decides. Suggested: `tiktok → postpeer`,
`instagram → instagram_graph` (free and uncapped by credits), `x → postpeer`
only if the 5-credit cost is accepted, otherwise leave X manual.

---

## 9. Media

**Cloudflare R2.** 10 GB free, and crucially **zero egress fees** — the media is
fetched by the platforms on every publish, and egress is what makes the
alternatives expensive.

- Content-address by SHA-256: `media/<sha256><ext>`. Re-enqueueing the same file
  never re-uploads, enforced by the `UNIQUE (project_id, sha256)` constraint.
- Serve from a custom domain (`media.<product-domain>`), **not** the `r2.dev`
  subdomain, which is rate-limited and documented as unsuitable for production.
- 10 GB is roughly 160 videos at current sizes. **Prune published media on a
  schedule** rather than discovering the ceiling at 90%.

---

## 10. SDK

What `video-maker` imports. It writes to Postgres directly -- there is no HTTP
API in v1, and the only producer is Python on the same machine. The signature is
what matters: it stays the same if a transport is ever added underneath it.

```python
from socialq import Client

sq = Client()                     # DATABASE_URL, R2_* from env
sq.enqueue(
    project="deploysafe",
    pipeline="brainrot",
    external_id="api-no-auth",
    media=["output/brainrot/api-no-auth.mp4"],
    caption={"default": "..."},
    cta={"instagram": "link in bio"},
    hashtags={"instagram": ["#vibecoding"]},
    surface={"instagram": "post"},
    aigc=True,
    at="2026-09-22T14:00:00Z",
)
```

Inside, in one transaction: hash each file, upload to R2 if the hash is new,
insert `media`, insert `posts`, resolve each platform to an account, insert one
`targets` row each. A partial enqueue must be impossible.

`enqueue` is **idempotent** on `(project, pipeline, external_id)` — re-running a
batch must not double-post.

Validate at enqueue time, in front of the caller: caption length per platform,
media count and aspect ratio for carousels, unknown platform. Failing here beats
failing in the worker six hours later with nobody watching.

---

## 11. Runtime

**Modal.** Python-native, scale-to-zero, no server to keep alive, and the free
tier covers this volume. Two functions:

- `worker` — cron every minute: claim, publish, record.
- `reconcile` — cron every 10 minutes: §7, plus token refresh.

Rendering stays in `video-maker` and never runs here, so the image is small — no
torch, no ffmpeg.

GitHub Actions is the wrong tool: 5-minute minimum granularity, frequent queue
delays, and nowhere for a reconciliation loop to live.

---

## 12. Build order

1. **Schema + migrations.** Get §4 committed before anything else.
2. **R2 + media layer.** Content-addressed upload, verified by URL fetch.
3. **Publisher interface + InstagramGraph.** Instagram first: it is free, has no
   credit budget, and the whole flow can be exercised against your own account.
4. **PostPeerTikTok.** Then post one real video and confirm it is public from a
   logged-out browser.
5. **Worker**: claim → publish → record → backoff.
6. **Reconciliation** and token refresh.
7. **SDK**, and wire `video-maker` to call it.

Steps 1–2 are prerequisites for everything. 3 before 4 deliberately: debug the
state machine against the free, uncapped platform.

**Not in v1:** an HTTP API. The only producer is `video-maker`, which is Python
and runs on the same machine, so it imports the SDK directly. An API would add a
deploy, an auth surface and a second thing that can be down, to serve a caller
that does not need it. It earns its place only if a producer appears that cannot
import Python -- and because the SDK signature (§10) would not change, adding it
then is an internal change to one file.

---

## 13. Testing

- **Publishers**: record real HTTP exchanges once, replay them in tests. Both
  APIs return errors in a 200 body, so a test that only asserts on status codes
  proves nothing.
- **Claim query**: two workers against one row, assert exactly one wins.
- **Crash recovery**: kill between §6 step 2 and step 3, assert reconciliation
  finds the post rather than double-posting. This is the test that matters most.
- **Idempotent enqueue**: same sidecar twice, assert one post and N targets.
- **End to end**: one real video to Instagram, on a real account. Do this before
  building the worker, not after.

---

## 14. Open questions and risks

- **Credits are the real ceiling, and the cheapest paid plan is $33/month.**
  Three rotated free accounts give 60 credits/month (§8.2.1), which is the plan.
  A target that cannot be published for want of credit waits for the next month;
  it does not fail.
- **Rotation depends on accounts that can vanish.** Nothing in their terms
  forbids holding several, but nothing protects them either. Losing one must
  degrade throughput, never stall the queue.
- **PostPeer's 10-item media library** is sidestepped by using R2 URLs, but if a
  code path ever uploads to them, it will silently fill and stick.
- **Vendor dependency.** If PostPeer goes away, TikTok stops until a replacement
  publisher is written. The publisher boundary makes this a contained change;
  keep it that way.
- **Token expiry is the most likely production failure.** Instagram's 60-day
  clock is the one thing that breaks with no code change. Alert on a refresh
  failure, loudly.
- **Account warming.** A brand new account that starts posting on a schedule is
  the shape platforms throttle. `accounts.enabled` exists so an account can be
  registered but held back; new accounts are warmed by hand for about a week.
- **Rate limits** are per platform and apply through any vendor: Instagram 50
  posts/24h.
- **A TikTok account under the handle `deployedunsafe` exists and is authorized
  for the API, but it is a test account scheduled for deletion.** Do not build
  against it. Credentials for it in `video-maker/.env` (`TIKTOK_*`) will stop
  working and can be removed.
- `video-maker/social/` and `tiktok_auth.py` hold a working TikTok OAuth flow,
  written before the PostPeer decision. Move them here as the basis of a future
  `NativeTikTok` publisher rather than deleting them.

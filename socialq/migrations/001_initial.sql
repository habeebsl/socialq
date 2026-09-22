-- Initial schema. See SOCIALQ_PLAN.md §4 and §8.2.1.
--
-- Everything carries project_id from the first commit, so a second product is
-- an INSERT rather than a migration.

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
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
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

-- §8.2.1 The credential is a pair, not a key: each PostPeer account connects
-- its own TikTok integration, so account_ids travels with the api key.
CREATE TABLE publisher_credentials (
  id             BIGSERIAL PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(id),
  publisher      TEXT NOT NULL,          -- "postpeer"
  label          TEXT NOT NULL,          -- which of the three
  api_key_ref    TEXT NOT NULL,          -- secret store key, never the secret
  account_ids    JSONB NOT NULL,         -- platform -> that account's id
  credits_left   INT,                    -- last seen, from GET /v1/usage/
  checked_at     TIMESTAMPTZ,
  expires_at     TIMESTAMPTZ,            -- instagram's 60-day token clock
  refreshed_at   TIMESTAMPTZ,
  enabled        BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (project_id, publisher, label)
);

CREATE TABLE attempts (
  id            BIGSERIAL PRIMARY KEY,
  target_id     BIGINT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  n             INT NOT NULL,
  idem_key      TEXT NOT NULL UNIQUE,     -- "target:<id>:<n>"
  -- §8.2.1(4): reconciliation must query the same account that posted.
  credential_id BIGINT REFERENCES publisher_credentials(id),
  request       JSONB,
  response      JSONB,
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ,
  UNIQUE (target_id, n)
);

CREATE INDEX ON targets (state, next_attempt_at);
CREATE INDEX ON posts (project_id, scheduled_for);

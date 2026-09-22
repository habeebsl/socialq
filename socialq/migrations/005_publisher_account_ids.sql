-- Which credential can post to which account, and under what id. §8.2.1.
--
-- THE PROBLEM. `publisher_credentials.account_ids` is JSON shaped
-- {platform -> id}, which holds exactly one id per platform per credential.
-- That is wrong for PostPeer in two directions at once:
--
--   * rotation means one TikTok account is reachable through three PostPeer
--     credentials, and each authorised TikTok separately, so each holds a
--     DIFFERENT integration id for the same account;
--   * a second TikTok account has nowhere to put its id, because the "tiktok"
--     key is already taken.
--
-- It is a grid -- (credential x account) -> id -- and JSON keyed by platform
-- can hold one cell of it.
--
-- Instagram has the same shape with one cell: one account, one token. There
-- the value of this table is the foreign key, which makes the pairing the
-- plan insists on (§8.2.1: "the credential is a pair, not a key") impossible
-- to get wrong, rather than merely matched by convention in code.
--
-- A missing row is meaningful: it says this credential cannot serve that
-- account. That is now queryable, instead of being discovered when the
-- rotation runs out of credit a third early.

CREATE TABLE publisher_account_ids (
  credential_id  BIGINT NOT NULL REFERENCES publisher_credentials(id)
                   ON DELETE CASCADE,
  account_id     BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  external_id    TEXT NOT NULL,        -- the id THIS credential uses for THAT account
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (credential_id, account_id)
);

CREATE INDEX ON publisher_account_ids (account_id);

ALTER TABLE publisher_account_ids ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON publisher_account_ids FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON publisher_account_ids FROM authenticated;
  END IF;
END
$$;

-- Backfill from the JSON. Existing rows pair a credential with whichever
-- account matches the id it already holds for that platform.
INSERT INTO publisher_account_ids (credential_id, account_id, external_id)
SELECT c.id, a.id, c.account_ids ->> a.platform
FROM publisher_credentials c
JOIN accounts a
  ON a.project_id = c.project_id
 AND c.account_ids ? a.platform
 AND (a.external_id IS NULL OR a.external_id = c.account_ids ->> a.platform)
WHERE c.account_ids ->> a.platform IS NOT NULL
ON CONFLICT DO NOTHING;

-- account_ids stays for now as a deprecated mirror, so a half-deployed
-- rollout keeps working. Drop it once nothing reads it.
COMMENT ON COLUMN publisher_credentials.account_ids IS
  'DEPRECATED: superseded by publisher_account_ids. Kept for rollback only.';

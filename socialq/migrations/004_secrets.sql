-- A writable secret store. §8.3, §8.2.1.
--
-- publisher_credentials.api_key_ref points here rather than holding a secret,
-- as the plan requires. The indirection stays; what changes is that the thing
-- it points at is now durable and writable on any runtime.
--
-- Why not the environment: §8.3's token refresh must write the new 60-day
-- token back somewhere, and env vars are read-only. Why not Modal's Dict: it
-- ties us to Modal, and its entries expire after 7 days of inactivity.
--
-- Values are encrypted with a key that lives outside the database
-- (SOCIALQ_SECRET_KEY), so a leaked dump is not a leaked Instagram account.

CREATE TABLE secrets (
  ref         TEXT PRIMARY KEY,           -- "DEPLOYEDUNSAFE_IG_TOKEN"
  value       BYTEA NOT NULL,             -- Fernet ciphertext, never plaintext
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE secrets ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON secrets FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON secrets FROM authenticated;
  END IF;
END
$$;

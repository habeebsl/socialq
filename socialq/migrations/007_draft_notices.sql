-- One row per draft whose caption has been emailed.
--
-- TikTok's video draft endpoint (/v2/post/publish/inbox/video/init/) accepts
-- only the file: no title, no caption. The text has to be pasted in the app,
-- so the worker emails it. This table is what makes that send-once and
-- retry-until-sent: a delivered draft with no row here is still owed an email.
--
-- A table of its own, NOT a column on targets: the worker builds Target from
-- `RETURNING t.*`, so a new targets column would crash whichever worker is
-- deployed when the migration runs -- for every account, not just drafts.

CREATE TABLE draft_notices (
  target_id  BIGINT PRIMARY KEY REFERENCES targets(id) ON DELETE CASCADE,
  sent_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Drafts delivered before this existed were captioned by hand already.
INSERT INTO draft_notices (target_id)
SELECT t.id FROM targets t JOIN accounts a ON a.id = t.account_id
WHERE a.draft AND t.state = 'published';

ALTER TABLE draft_notices ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON draft_notices FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON draft_notices FROM authenticated;
  END IF;
END
$$;

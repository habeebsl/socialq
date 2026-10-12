-- Per-account draft mode: a post to this account lands in the creator's drafts
-- instead of going public.
--
-- Per account rather than a worker flag because the worker serves every
-- account at once. A worker-wide switch would turn @deployedunsafe's live
-- Instagram posts into drafts along with the one account meant to be held.
--
-- Default false so every existing account keeps posting exactly as before.

ALTER TABLE accounts ADD COLUMN draft BOOLEAN NOT NULL DEFAULT false;

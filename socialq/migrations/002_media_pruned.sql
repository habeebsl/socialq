-- §9: "Prune published media on a schedule rather than discovering the ceiling
-- at 90%." 10 GB is roughly 160 videos at current sizes.
--
-- The row stays after the object is deleted: it is the record that the file
-- existed and what its hash was, and media_ids on posts still points at it.
-- Only the bytes go.

ALTER TABLE media ADD COLUMN pruned_at TIMESTAMPTZ;

CREATE INDEX ON media (pruned_at) WHERE pruned_at IS NULL;

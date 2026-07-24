-- Feature 3 (person-level dedup reporting): when a POC is skipped as a
-- duplicate, record a human-readable note of WHO/WHAT it collided with (the
-- prior contacted_pocs row's poc_name/firm_name/date) so the final summary
-- and the per-chunk enrichment message can report skips by name, not just a
-- bare count. Nullable — only set for email_status='skipped_duplicate' rows.
ALTER TABLE wc_drafts ADD COLUMN IF NOT EXISTS collision_note TEXT;

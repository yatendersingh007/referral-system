-- Feature B (/cancel): a persistent flag on wc_runs so a cancellation
-- request survives the drain loop's between-firm checks AND a bot restart
-- (an in-memory flag would be lost on either). Nullable timestamp (not
-- boolean) so WHEN it was requested is auditable, matching wc_drafts's own
-- collision_note "nullable, only set when it actually happened" pattern.
--
-- wc_runs.status gains a new value this migration doesn't need to declare
-- (the column has always been unconstrained TEXT, see migrations/002): a
-- cancelled run's status becomes 'cancelled', distinct from 'done'/'crashed'.
ALTER TABLE wc_runs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

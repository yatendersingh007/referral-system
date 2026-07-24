-- icp_scoring_v2_2.md requires every gated firm carry an AUM confidence tag
-- (AUM_CONFIRMED | AUM_ESTIMATED | AUM_UNVERIFIED) so the proxy basis is
-- auditable — mirrors companies.aum_tag in Workflow A's schema. Missed in
-- the initial §5 migration.
ALTER TABLE wc_firms ADD COLUMN IF NOT EXISTS aum_tag TEXT;

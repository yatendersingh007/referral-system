-- Workflow C (Referral Outreach) schema additions.
-- Applied via `python -m lib_snapshot.db migrate` (non-destructive; tracked by
-- filename in schema_migrations). Additive only — never touches Workflow A's
-- existing tables (companies, leads, client_profiles, etc.).

CREATE TABLE IF NOT EXISTS connections (
  id SERIAL PRIMARY KEY,
  linkedin_url TEXT UNIQUE NOT NULL,
  poc_name TEXT NOT NULL,
  designation TEXT,
  company_name_raw TEXT,          -- exactly as in the file
  company_name_norm TEXT,         -- normalized for matching
  poc_email_seed TEXT,            -- Email Address column, when present in the export
  incomplete_row BOOLEAN DEFAULT FALSE,
  loaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS connections_company_norm_idx ON connections (company_name_norm);

CREATE TABLE IF NOT EXISTS wc_runs (
  id SERIAL PRIMARY KEY,
  mode TEXT NOT NULL,             -- 'single_firm' | 'batch'
  query_firm TEXT,                -- null in batch mode
  started_at TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT DEFAULT 'running',  -- running | done | crashed
  crash_detail TEXT
);

CREATE TABLE IF NOT EXISTS wc_queue (
  id SERIAL PRIMARY KEY,
  run_id INT REFERENCES wc_runs(id),
  firm_name_input TEXT NOT NULL,       -- as supplied by user
  queue_status TEXT DEFAULT 'pending', -- pending | no_connection | processing | done | infra_paused
  position INT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wc_firms (
  id SERIAL PRIMARY KEY,
  run_id INT REFERENCES wc_runs(id),
  firm_name TEXT NOT NULL,
  match_method TEXT,              -- 'exact' | 'normalized' | 'llm'
  match_confidence NUMERIC,
  icp_status TEXT,                -- qualified | disqualified | parked
  icp_reason TEXT,
  research_confidence NUMERIC,
  source_count INT,
  search_calls INT DEFAULT 0,
  tokens_used INT DEFAULT 0,
  research_cost_usd NUMERIC DEFAULT 0,
  park_reason TEXT,               -- firm_match_ambiguous | aum_unverified | budget_cap_hit | ...
  park_source TEXT,               -- 'research' | 'infrastructure'
  research_raw JSONB,             -- Phase 2 research object, reused by Phase 4/5
  domain TEXT,                    -- resolved during Phase 2, fed to Apollo as a match hint
  top_poc_connection_id INT REFERENCES connections(id),  -- Phase 3 ranking winner
  alternate_pocs JSONB,           -- other matched connections, ranked, for future use
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wc_drafts (
  id SERIAL PRIMARY KEY,
  firm_id INT REFERENCES wc_firms(id),
  connection_id INT REFERENCES connections(id),
  poc_email TEXT,
  email_status TEXT,              -- drafted | no_email_found | skipped_duplicate
  thin_personalization BOOLEAN DEFAULT FALSE,
  personalization JSONB,          -- hooks with source URLs
  gmail_draft_id TEXT,
  ppt_attached BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- SHARED across workflows A and C.
CREATE TABLE IF NOT EXISTS contacted_pocs (
  id SERIAL PRIMARY KEY,
  linkedin_url TEXT,
  email TEXT,
  poc_name TEXT,
  firm_name TEXT,
  source_workflow TEXT NOT NULL,  -- 'A' | 'C'
  gmail_draft_id TEXT,
  contacted_at TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS contacted_pocs_url_idx ON contacted_pocs (linkedin_url) WHERE linkedin_url IS NOT NULL;

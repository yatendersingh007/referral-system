-- QA finding: /referral reload calls dbc.clear_connections() (DELETE FROM
-- connections) before reinserting the refreshed CSV. wc_firms.top_poc_connection_id
-- and wc_drafts.connection_id referenced connections(id) with the Postgres
-- default ON DELETE NO ACTION — so the very first reload after ANY firm has
-- ever been drafted would crash with a ForeignKeyViolation, permanently
-- breaking the spec'd "/referral reload — re-ingest data/connections.csv
-- (after Manpreet's export is refreshed)" command. Historical wc_firms/
-- wc_drafts rows are an audit trail and must survive a connections refresh;
-- the specific connection_id reference going null on reload is correct and
-- harmless (the firm_name/poc_email/etc columns already carry what's needed
-- for the audit trail independent of the connections row).
ALTER TABLE wc_firms DROP CONSTRAINT IF EXISTS wc_firms_top_poc_connection_id_fkey;
ALTER TABLE wc_firms ADD CONSTRAINT wc_firms_top_poc_connection_id_fkey
  FOREIGN KEY (top_poc_connection_id) REFERENCES connections(id) ON DELETE SET NULL;

ALTER TABLE wc_drafts DROP CONSTRAINT IF EXISTS wc_drafts_connection_id_fkey;
ALTER TABLE wc_drafts ADD CONSTRAINT wc_drafts_connection_id_fkey
  FOREIGN KEY (connection_id) REFERENCES connections(id) ON DELETE SET NULL;
